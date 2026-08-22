"""Composite-answerer: шаблоны -> LLM -> очередь (#482).

Связывает чистый резолвер с хранилищем и с pipeline отклика. Наружу выставляет
ровно тот контракт, который pipeline уже использует для ``AIQuestionAnswerer``
(``propose_all`` и ``apply``), поэтому подключение не требует новой ветки в
оркестраторе — только выбора объекта в билдере.

Почему composite строится ДАЖЕ БЕЗ AI: ``pipeline._run`` пропускает вакансию с
анкетой, если ``question_answerer is None``. При целевой конфигурации #482
(``questionnaires.enabled: true`` + ``ai.answer_questions: false``) старый
билдер вернул бы None, и keyword resolver никогда бы не запустился — критерий
приёмки «работает без AI-зависимости» был бы недостижим. Поэтому LLM здесь
опционален: без него работают static-шаблоны и подтверждённые формулировки, а
всё остальное штатно уходит в очередь.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

from ..ai.questions import AIQuestionAnswerer, AnswerProposal, Question
from ..external_forms.detect import normalize
from .resolver import LLM, ResolvedAnswer, TemplateMatch, build_answer, resolve_template
from .templates import DEFAULT_CLUSTER, QuestionTemplate, cluster_for, is_strict

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from ..config_sections.questionnaires import QuestionnairesConfig
    from ..history import History

logger = logging.getLogger("hhru_bot.questionnaires.answerer")


def confirm_mapping(
    *,
    learn: bool,
    prompt: str,
    isatty_fn=None,
    input_fn=input,
) -> bool:
    """Подтверждение первого LLM-сопоставления вопроса с шаблоном (#482).

    Намеренно НЕ переиспользует ``commands.copy_resume.confirm_write``, хотя
    форма та же: та функция возвращает True при ``--force``, а ``--force`` в
    этом проекте авторизует ОТПРАВКУ отклика, а не обучение. Приняв её как есть,
    боевой ``apply --force`` начал бы молча узаконивать догадки модели — прямо
    против решения issue «первое LLM-сопоставление требует подтверждения
    пользователя». Разрешение на обучение даёт только ``--learn-questionnaires``.

    Без флага вопрос вообще не задаётся: batch не должен вставать на stdin.
    В headless/не-TTY (в том числе cron) ответ всегда отрицательный — вопрос
    уходит в очередь, вакансия пропускается, прогон продолжается.
    """
    if not learn:
        return False
    if not (isatty_fn or sys.stdin.isatty)():
        return False
    return input_fn(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes", "д", "да")


def _template_from_row(name: str, row: dict) -> QuestionTemplate:
    return QuestionTemplate(
        name=name,
        cluster=row.get("cluster") or DEFAULT_CLUSTER,
        mode=row.get("mode") or "static",
        answer=row.get("answer"),
        instruction=row.get("instruction"),
        examples=tuple(row.get("examples") or ()),
    )


class TemplateQuestionAnswerer:
    """Отвечает на вопросы анкеты по обучаемым шаблонам.

    Ступени на каждый вопрос:
      1. подтверждённая формулировка или ключевые слова (без AI);
      2. LLM-сопоставление с известным шаблоном — требует подтверждения
         пользователя и включается только флагом ``--learn-questionnaires``;
      3. всё нерешённое — в очередь; вопрос возвращается с нулевой уверенностью,
         и существующий путь pipeline сам пропускает вакансию без отправки.
    """

    def __init__(
        self,
        history: History,
        resume_id: str,
        *,
        settings: QuestionnairesConfig,
        llm=None,
        llm_fallback: AIQuestionAnswerer | None = None,
        learn: bool = False,
        confirm_fn=None,
    ):
        self._history = history
        self._resume_id = resume_id
        self._settings = settings
        self._llm = llm
        self._fallback = llm_fallback
        self._learn = learn
        self._confirm = confirm_fn or confirm_mapping
        # Шаблоны и подтверждённые формулировки читаются один раз на прогон
        # резюме: они меняются только командой questionnaire, которая под общим
        # write-lock не может выполняться параллельно с apply.
        self._templates = history.get_questionnaire_templates(resume_id)
        self._phrases = history.get_confirmed_phrases(resume_id)
        self._pending: list[dict] = []

    def _cluster_of(self, template: str) -> str:
        """Кластер шаблона: сохранённый оператором, иначе seed-значение."""
        row = self._templates.get(template)
        if row and row.get("cluster"):
            return str(row["cluster"])
        return cluster_for(template)

    @property
    def pending(self) -> list[dict]:
        """Нерешённые вопросы, накопленные за прогон (для записи в очередь)."""
        return list(self._pending)

    def _queue(self, question: Question, resolved: ResolvedAnswer) -> AnswerProposal:
        match = resolved.match
        self._pending.append(
            {
                "text": question.text,
                "kind": question.kind,
                "is_radio": question.is_radio,
                "options": list(question.options),
                "template": match.template if match else None,
                "cluster": match.cluster if match else None,
                "reason": resolved.pending_reason,
            }
        )
        logger.info(
            "[skip] Вопрос анкеты в очереди: %s — %s", question.text, resolved.pending_reason
        )
        # threshold не занижаем: нулевая уверенность ниже любого порога, и
        # pipeline трактует такой proposal как «не заполнять».
        return AnswerProposal(
            question,
            "",
            0.0,
            threshold=self._settings.llm_answer_threshold,
            template=match.template if match else None,
            cluster=match.cluster if match else None,
            resolver_source=resolved.resolver_source,
        )

    def _llm_match(self, question: Question) -> TemplateMatch | None:
        """Сопоставление вопроса с ИЗВЕСТНЫМ шаблоном силами модели.

        Модель выбирает имя шаблона из уже сохранённых — она не придумывает ни
        новый шаблон, ни сам ответ. Это тот же приём, что в
        ``external_forms.match_answer_llm``: наружу уходят только имена полей,
        а значение подставляется локально.
        """
        if self._llm is None or not self._templates:
            return None
        from ..external_forms.detect import match_answer_llm

        # match_answer_llm возвращает ЗНАЧЕНИЕ по ключу, поэтому подаём ему
        # словарь «имя шаблона -> имя шаблона»: тогда его ответ и есть имя
        # выбранного шаблона, а порог/денилист/валидация JSON переиспользуются
        # без копирования.
        names = {name: name for name in self._templates}
        try:
            chosen = match_answer_llm(question.text, names, self._llm)
        except Exception as exc:  # noqa: BLE001 — сбой модели не рвёт отклик
            logger.warning("Анкета: LLM-сопоставление не удалось (%s)", exc)
            return None
        if not chosen or chosen not in self._templates:
            return None
        return TemplateMatch(
            chosen, self._cluster_of(chosen), LLM, self._settings.llm_match_threshold
        )

    def _resolve(self, question: Question) -> ResolvedAnswer:
        match = resolve_template(question.text, confirmed=self._phrases)
        if match is None:
            match = self._llm_match(question)
            if match is None:
                return ResolvedAnswer.pending("вопрос не сопоставлен ни с одним шаблоном")
            if is_strict(self._cluster_of(match.template)):
                # Комплаенс-вопрос не имеет права опираться на догадку модели
                # даже с подтверждением: подтверждается сопоставление, а не
                # факт о документах. Отвечает только явный static-шаблон,
                # найденный детерминированно, — см. resolver.compliance_gate.
                return ResolvedAnswer.pending(
                    "комплаенс-вопрос не сопоставляется автоматически", match
                )
            # Первое сопоставление моделью пользователь должен подтвердить: без
            # этого одна ошибка классификации молча закрепилась бы как факт и
            # повторялась на каждой следующей вакансии.
            if not self._confirm(
                learn=self._learn,
                prompt=f'Вопрос "{question.text}" -> шаблон "{match.template}"? Подтвердить',
            ):
                return ResolvedAnswer.pending(
                    f"LLM предлагает шаблон '{match.template}', "
                    "нужно подтверждение (--learn-questionnaires)",
                    match,
                )
            self._history.confirm_questionnaire_example(
                match.template, question.text, resume_id=self._resume_id, confirmed_by="user"
            )
            # Тот же вопрос в этом же прогоне (другая вакансия — та же
            # формулировка) должен решаться уже без повторного вопроса
            # пользователю, поэтому кэш обновляется сразу, а не только в БД.
            self._phrases[normalize(question.text)] = match.template
        row = self._templates.get(match.template)
        template = _template_from_row(match.template, row) if row else None
        # Кластер сохранённого шаблона — источник истины и перекрывает тот, что
        # стратегия вывела из seed-списка: оператор мог явно объявить свой
        # шаблон комплаенсным (`questionnaire set ... --cluster compliance`), а
        # seed-таблица о нём ничего не знает и вернула бы 'mixed'. Без этой
        # синхронизации строгий гейт молча не срабатывал бы ровно там, где он
        # нужнее всего — на пользовательских комплаенс-шаблонах.
        match = replace(match, cluster=self._cluster_of(match.template))
        return build_answer(
            question,
            template,
            match,
            llm=self._llm,
            answer_threshold=self._settings.llm_answer_threshold,
        )

    def _fallback_proposal(self, question: Question) -> AnswerProposal | None:
        """Последняя LLM-ступень цепочки #482 перед вопросом пользователю.

        Часть вопросов анкеты принципиально не сводится к шаблону: проверка
        навыков и задачи под конкретную вакансию уникальны, заводить под каждую
        свой шаблон бессмысленно. Для них работает уже существующий
        two-stage answerer (#473): факт из профиля аккаунта, иначе генерация.

        Комплаенс сюда не попадает — он отсеян раньше в ``_resolve``. Порог
        применяется наш (0.90 по умолчанию), а не дефолтные 0.70 LLM-пути:
        ответ, сочинённый без шаблона, должен проходить более высокую планку.
        """
        if self._fallback is None:
            return None
        proposal = self._fallback.propose(question)
        if not proposal.answer and not proposal.option_indices:
            return None
        return AnswerProposal(
            question,
            proposal.answer,
            proposal.confidence,
            proposal.option_indices,
            proposal.answer_source,
            threshold=self._settings.llm_answer_threshold,
            resolver_source="fallback",
        )

    def propose(self, question: Question) -> AnswerProposal:
        resolved = self._resolve(question)
        if resolved.match is None and not resolved.resolved:
            fallback = self._fallback_proposal(question)
            if fallback is not None and not fallback.low_confidence:
                return fallback
        if not resolved.resolved:
            return self._queue(question, resolved)
        match = resolved.match
        return AnswerProposal(
            question,
            resolved.answer,
            resolved.confidence,
            resolved.option_indices,
            resolved.answer_source,
            threshold=self._settings.llm_answer_threshold,
            template=match.template if match else None,
            cluster=match.cluster if match else None,
            resolver_source=resolved.resolver_source,
        )

    def propose_all(self, questions: list[Question]) -> list[AnswerProposal]:
        return [self.propose(question) for question in questions]

    @staticmethod
    def apply(page: Page, proposals: list[AnswerProposal]) -> list[AnswerProposal]:
        """Заполнение формы — целиком переиспользованное поведение #97/#373.

        Оно уже умеет radio/checkbox/textarea и пропускает неуверенные ответы;
        второй реализации того же не нужно.
        """
        return AIQuestionAnswerer.apply(page, proposals)

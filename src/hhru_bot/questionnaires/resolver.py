"""Цепочка стратегий сопоставления вопрос -> шаблон -> ответ (#482).

Чистые функции: ни браузера, ни SQLite. LLM попадает сюда только как уже
готовый клиент в аргументе ``llm`` — модуль его не конструирует и не
импортирует, поэтому весь keyword-путь работает при отсутствующей
optional-зависимости ``.[ai]``.

Цепочка #482 — ``ключевые слова -> будущий ML-слот -> LLM -> вопрос
пользователю``. Здесь реализованы первая и третья ступени; ML-слот обозначен
явной точкой в ``resolve_template`` (стратегии и ML-зависимостей в этой версии
нет по решению issue).

Общий инвариант всех функций модуля: при любом сомнении возвращается причина
для очереди (``pending_reason``), а не догадка. Неотвеченный вопрос стоит одной
пропущенной вакансии; неверный ответ отправляется работодателю навсегда.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..ai.questions import Question
from ..external_forms.detect import normalize
from .templates import (
    SEED_TEMPLATES,
    QuestionTemplate,
    SeedTemplate,
    TemplateError,
    is_compliance_text,
    is_strict,
)

if TYPE_CHECKING:
    from ..ai.llm_client import LLMClient

logger = logging.getLogger("hhru_bot.questionnaires.resolver")

#: Источники сопоставления. ``phrase`` — подтверждённая пользователем
#: формулировка (точное совпадение), ``keyword`` — seed/пользовательские
#: ключевые слова, ``llm`` — сопоставление моделью, требующее подтверждения.
PHRASE = "phrase"
KEYWORD = "keyword"
LLM = "llm"

#: Уверенность keyword-стратегии. Ниже подтверждённой формулировки (1.0):
#: точное совпадение с однажды подтверждённым текстом — факт, а попадание
#: ключевого слова — всё же эвристика. Значение выше любого разумного
#: ``llm_match_threshold``, поэтому keyword-матч никогда не отсекается порогом,
#: предназначенным для LLM.
KEYWORD_CONFIDENCE = 0.95

#: Максимум подтверждённых примеров в contextual-промпте. Порядок берётся из
#: хранилища (``ORDER BY id``), то есть детерминирован: один и тот же вопрос
#: даёт один и тот же промпт между запусками.
MAX_PROMPT_EXAMPLES = 5

#: Сколько LLM-ответов на анкету допустимо ждать. Совпадает по смыслу с
#: таймаутом скоринга (``scoring._LLM_TIMEOUT``): анкета заполняется в живой
#: сессии браузера, зависший запрос держал бы открытую форму отклика.
LLM_TIMEOUT = 30.0
_LLM_MAX_TOKENS = 512


@dataclass(frozen=True)
class TemplateMatch:
    """Вопрос отнесён к шаблону. Ещё не ответ — только смысл вопроса."""

    template: str
    cluster: str
    source: str
    confidence: float


@dataclass(frozen=True)
class ResolvedAnswer:
    """Итог резолва: либо готовый ответ, либо причина отправить вопрос в очередь.

    ``answer_source`` намеренно ограничен парой ``profile``/``llm`` — это
    закрытое множество, на которое опирается агрегат
    ``History.questionnaire_answer_summary`` (и через него ``stats``). Смысл
    поля — «локальный факт против генерации»: static-шаблон и keyword-матч дают
    ``profile``, ответ, сочинённый моделью под конкретную вакансию, — ``llm``.
    Детализация (какой именно шаблон, какой кластер, какая стратегия сработала)
    живёт в отдельных полях и попадает в аудит собственными колонками.
    """

    match: TemplateMatch | None = None
    answer: str = ""
    option_indices: tuple[int, ...] = ()
    answer_source: str = "profile"
    confidence: float = 0.0
    resolver_source: str = ""
    pending_reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.match is not None and not self.pending_reason

    @classmethod
    def pending(cls, reason: str, match: TemplateMatch | None = None) -> ResolvedAnswer:
        return cls(match=match, pending_reason=reason)


def match_phrase(text: str, confirmed: dict[str, str]) -> TemplateMatch | None:
    """Точное совпадение нормализованного текста с подтверждённой формулировкой.

    Первая и самая дешёвая ступень: если пользователь однажды подтвердил, что
    этот вопрос означает этот шаблон, ни ключевые слова, ни LLM больше не
    нужны. Это же множество — накапливаемая обучающая выборка для будущего ML.
    """
    template = confirmed.get(normalize(text))
    if not template:
        return None
    from .templates import cluster_for

    return TemplateMatch(template, cluster_for(template), PHRASE, 1.0)


def match_keyword(
    text: str, seeds: Sequence[SeedTemplate] = SEED_TEMPLATES
) -> TemplateMatch | None:
    """Keyword resolver: ключевое слово шаблона входит в текст вопроса.

    Fail-closed на неоднозначности: совпадение засчитывается, только если оно
    указывает РОВНО на один шаблон. Вопрос вида «в каком городе вы готовы
    работать и на какую зарплату рассчитываете» задевает и ``location``, и
    ``salary``; ответить на него одним значением нельзя, а выбрать «первый
    попавшийся» шаблон означало бы отправить работодателю ответ не на тот
    вопрос. Такой вопрос уходит в очередь и решается человеком один раз.
    """
    normalized = normalize(text)
    matched = {seed.name: seed for seed in seeds if any(k in normalized for k in seed.keywords)}
    if len(matched) != 1:
        if matched:
            logger.debug(
                "Анкета: вопрос %r задевает несколько шаблонов (%s) — сопоставление снято",
                text,
                ", ".join(sorted(matched)),
            )
        return None
    seed = next(iter(matched.values()))
    return TemplateMatch(seed.name, seed.cluster, KEYWORD, KEYWORD_CONFIDENCE)


def resolve_template(
    text: str,
    *,
    confirmed: dict[str, str],
    seeds: Sequence[SeedTemplate] = SEED_TEMPLATES,
) -> TemplateMatch | None:
    """Сопоставление без AI: подтверждённая формулировка, затем ключевые слова.

    Порядок не произволен: подтверждённая формулировка — решение пользователя и
    должна перекрывать эвристику, иначе однажды исправленное сопоставление
    молча возвращалось бы к keyword-варианту на каждом прогоне.

    ML-слот (#482) — здесь, между keyword и LLM: обученный классификатор
    получал бы вопросы, которые не разобрали две детерминированные стратегии, и
    отдавал бы такой же ``TemplateMatch`` со своим ``source``. В этой версии
    слот пуст намеренно (ML и его зависимости issue исключает), поэтому
    нерешённое здесь уходит выше — в LLM-ступень ``answerer``.
    """
    return match_phrase(text, confirmed) or match_keyword(text, seeds)


def compliance_gate(
    template: QuestionTemplate | None,
    match: TemplateMatch | None = None,
    *,
    text: str = "",
) -> str:
    """Пустая строка, если отвечать разрешено; иначе причина для очереди.

    #482: «Документы и комплаенс отвечаются только явным сохранённым
    значением». Для строгих кластеров недостаточно того, что вопрос УЗНАН, —
    нужен сохранённый static-ответ. Ни contextual-генерация, ни keyword-догадка
    сюда не допускаются: ошибка в ответе про гражданство, судимость или
    разрешение на работу необратима и видна работодателю.

    Тема определяется ДВУМЯ независимыми признаками, и достаточно любого:
    кластер сопоставленного шаблона ИЛИ сам текст вопроса. Одной кластерной
    проверки мало — она работает только когда шаблон найден, а вопрос про
    судимость может не совпасть ни с одним шаблоном и тогда ушёл бы дальше по
    цепочке в свободную LLM-генерацию. Поэтому ``text`` проверяется и при
    ``match is None``.
    """
    strict = (match is not None and is_strict(match.cluster)) or is_compliance_text(text)
    if not strict:
        return ""
    name = f" (шаблон '{match.template}')" if match is not None else ""
    if template is None:
        return f"комплаенс-вопрос без сохранённого значения{name}"
    if not template.is_static or not (template.answer or "").strip():
        return f"комплаенс-вопрос допускает только явный static-ответ{name}"
    return ""


def choice_indices(question: Question, value: str) -> tuple[int, ...]:
    """Индексы вариантов, чей ТЕКСТ совпадает со значением ответа.

    #482: «Варианты сопоставляются по тексту, не по сохранённым индексам».
    Индекс варианта — свойство конкретной отрисовки страницы: hh.ru показывает
    один и тот же набор в разном порядке на разных вакансиях (это уже
    наблюдалось при группировке вопросов, см. ``apply/questionnaire.py``).
    Сохранённый индекс означал бы, что после перестановки вариантов бот
    уверенно выбирает не тот ответ.

    Значение может перечислять несколько вариантов через запятую или перевод
    строки — для checkbox-вопросов это единственный способ выбрать больше
    одного варианта сохранённым текстом.
    """
    wanted = {normalize(part) for part in value.replace("\n", ",").split(",") if part.strip()}
    if not wanted:
        return ()
    return tuple(i for i, option in enumerate(question.options) if normalize(option) in wanted)


def check_choice_compatibility(question: Question, indices: tuple[int, ...]) -> str:
    """Пустая строка, если выбор допустим для этого типа вопроса.

    #482: «Radio допускает один выбор, checkbox несколько; несовместимые
    варианты не заполняются». Проверка нужна отдельно от подсчёта индексов:
    браузер молча оставит последний отмеченный radio, если отметить несколько,
    то есть отправится не то, что было показано в предпросмотре и записано в
    аудит (ровно тот класс расхождения, что закрывали в ``ai/questions.py``).
    """
    if not indices:
        return "ни один вариант ответа не совпал с сохранённым значением"
    if question.is_radio and len(indices) != 1:
        return (
            f"вопрос допускает один вариант, а сохранённому значению соответствуют {len(indices)}"
        )
    return ""


def _static_answer(
    question: Question, template: QuestionTemplate, match: TemplateMatch
) -> ResolvedAnswer:
    answer = (template.answer or "").strip()
    indices: tuple[int, ...] = ()
    if question.kind == "choice":
        indices = choice_indices(question, answer)
        if reason := check_choice_compatibility(question, indices):
            return ResolvedAnswer.pending(reason, match)
    return ResolvedAnswer(
        match=match,
        answer=answer,
        option_indices=indices,
        answer_source="profile",
        confidence=1.0,
        resolver_source="static",
    )


def _contextual_prompt(question: Question, template: QuestionTemplate) -> list[dict[str, str]]:
    system = (
        "Ты отвечаешь на вопрос анкеты работодателя на hh.ru от лица кандидата. "
        "Следуй инструкции кандидата буквально и не выдумывай фактов о нём. "
        "Ответь только JSON без markdown: "
        '{"answer":"...","confidence":0.0,"indices":[0]}. '
        "confidence — твоя уверенность от 0 до 1."
    )
    parts = [f"Инструкция кандидата: {template.instruction}", f"Вопрос: {question.text}"]
    if question.kind == "choice":
        options = "\n".join(f"{i}: {value}" for i, value in enumerate(question.options))
        parts.append(
            "Выбери подходящие варианты по индексам (с нуля).\n"
            f"Варианты:\n{options}"
            + (
                "\nДопустим ровно один индекс."
                if question.is_radio
                else "\nДопустим один или несколько индексов."
            )
        )
    else:
        parts.append("Сгенерируй краткий правдивый ответ на русском языке.")
    if template.examples:
        shown = "\n".join(f"- {text}" for text in template.examples[:MAX_PROMPT_EXAMPLES])
        parts.append(f"Так выглядят вопросы этого же смысла:\n{shown}")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def parse_llm_payload(raw: str) -> tuple[dict, float]:
    """Разобрать JSON модели и её уверенность. Любое нарушение — исключение.

    Общая для ОБОИХ LLM-путей (сопоставление шаблона и генерация ответа) —
    инвариант обязан быть одинаковым: ``float()`` принимает литералы
    ``NaN``/``Infinity``, а сравнение ``nan < threshold`` в Python ложно, то
    есть испорченная уверенность прошла бы порог как ВЫСОКАЯ и была бы
    отправлена. Тот же контракт, что в ``ai/questions.py`` (#373).
    """
    payload = json.loads(raw.strip())
    if not isinstance(payload, dict):
        raise ValueError("LLM вернул не JSON-объект")
    confidence = float(payload.get("confidence", 0))
    if not (math.isfinite(confidence) and 0.0 <= confidence <= 1.0):
        raise ValueError(f"LLM вернул некорректную уверенность: {confidence!r}")
    return payload, confidence


def _parse_llm_answer(question: Question, raw: str) -> tuple[str, tuple[int, ...], float]:
    """Строгий разбор ответа модели на вопрос анкеты."""
    payload, confidence = parse_llm_payload(raw)
    answer = str(payload.get("answer", "")).strip()
    indices = tuple(int(i) for i in payload.get("indices", []))
    if question.kind == "choice":
        if not indices:
            raise ValueError("LLM не выбрал ни одного варианта")
        if any(i < 0 or i >= len(question.options) for i in indices):
            raise ValueError("LLM вернул индекс варианта вне диапазона")
        if reason := check_choice_compatibility(question, indices):
            raise ValueError(reason)
        if not answer:
            answer = ", ".join(question.options[i] for i in indices)
    elif not answer:
        raise ValueError("LLM вернул пустой текстовый ответ")
    return answer, indices, confidence


def _contextual_answer(
    question: Question,
    template: QuestionTemplate,
    match: TemplateMatch,
    *,
    llm: LLMClient | None,
    answer_threshold: float,
) -> ResolvedAnswer:
    if llm is None:
        return ResolvedAnswer.pending(
            f"шаблон '{template.name}' contextual, но LLM не настроен "
            "(включите ai.answer_questions или задайте static-ответ)",
            match,
        )
    try:
        response = llm.chat(
            _contextual_prompt(question, template),
            temperature=0.2,
            max_tokens=_LLM_MAX_TOKENS,
            timeout=LLM_TIMEOUT,
        )
        answer, indices, confidence = _parse_llm_answer(question, response.content or "")
    except Exception as exc:  # noqa: BLE001 — сбой модели не должен рвать отклик
        # Тот же инвариант, что у LLMScoringProvider.score и AICoverLetterProvider:
        # ошибка LLM деградирует в отсутствие ответа, а не в исключение наружу.
        logger.warning("Анкета: contextual-ответ не получен (%s) — вопрос в очередь", exc)
        return ResolvedAnswer.pending(f"LLM не дал пригодного ответа: {exc}", match)
    if confidence < answer_threshold:
        return ResolvedAnswer.pending(
            f"уверенность ответа {confidence:.2f} ниже порога {answer_threshold:.2f}", match
        )
    return ResolvedAnswer(
        match=match,
        answer=answer,
        option_indices=indices,
        answer_source="llm",
        confidence=confidence,
        resolver_source="contextual",
    )


def build_answer(
    question: Question,
    template: QuestionTemplate | None,
    match: TemplateMatch,
    *,
    llm: LLMClient | None = None,
    answer_threshold: float = 0.90,
) -> ResolvedAnswer:
    """Построить ответ по шаблону или объяснить, почему это невозможно.

    Комплаенс-гейт проверяется первым: он должен срабатывать и тогда, когда
    шаблон вовсе не сохранён (``template is None``), и тогда, когда он есть, но
    в contextual-режиме.
    """
    if reason := compliance_gate(template, match, text=question.text):
        return ResolvedAnswer.pending(reason, match)
    if template is None:
        return ResolvedAnswer.pending(
            f"нет сохранённого ответа для шаблона '{match.template}'", match
        )
    try:
        template.validate()
    except TemplateError as exc:
        # Строка могла быть заведена вручную прямо в history.db (штатный для
        # проекта способ) — неисполнимый шаблон не должен валить весь прогон.
        return ResolvedAnswer.pending(f"шаблон непригоден: {exc}", match)
    if template.is_static:
        return _static_answer(question, template, match)
    return _contextual_answer(question, template, match, llm=llm, answer_threshold=answer_threshold)

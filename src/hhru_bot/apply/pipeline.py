"""Оркестратор отклика на вакансию.

Тонкая связка: открывает вакансию → проверяет дедупликацию → ждёт кнопку →
(dry-run стоп) → навигация на форму → заполнение → подтверждение успеха.
Каждый шаг живёт в своём модуле (dedup/steps/success/probe/letter) и принадлежит
конкретному feature-ишью. pipeline никем не трогается после Wave 0: feature-ишью
меняют внутренности шагов, а не последовательность.

Точки вызова ctx.probe пред-добавлены нейтрально (#8 их наполнит).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..ai.questions import AIQuestionAnswerer, AnswerProposal, extract_questions
from ..browser import goto_hh, has_login_form
from ..history import SKIP_REASONS, History
from ..search import VacancyCard
from . import steps as apply_steps
from .antibot import AntiBotChallengeDetected, detect_antibot_on_page
from .blockers import PostClickBlocker, PostSubmitLimitExceeded
from .dedup import check_already_responded
from .letter import VARIANT_TEMPLATE, CoverLetterProvider, render_cover_letter
from .probe import NOOP_PROBE, ProbeHook
from .questionnaire import question_cluster_key
from .questions import detect_questions
from .steps import SubmitClickUncertain
from .success import wait_success_confirmation
from .verify import ResponseVerifier

logger = logging.getLogger("hhru_bot.apply")


@dataclass
class ApplyResult:
    vacancy: VacancyCard
    success: bool
    reason: str = ""
    # A/B-вариант письма (#17): 'template' / 'ai' / 'ai_fallback'. Для записи в
    # history.actions.letter_variant. По умолчанию 'template' (без AI-провайдера).
    letter_variant: str = VARIANT_TEMPLATE
    # #95: skip — третий исход (помимо success/fail). True → отправки не было
    # (вопросы в форме #95 или уже существующий отклик #226). В отличие от fail,
    # skip не пишет status='failed' в actions и не расходует дневной лимит/троттл
    # (см. commands/_common.run_apply_for_resume).
    skipped: bool = False
    # #226 cycle-review: persistent skip-причина для history.record_skip
    # (SKIP_REASONS.*). Раньше run_apply_for_resume жёстко писал HAS_QUESTIONS
    # для ЛЮБОГО skipped=True — already-responded-skip терялся под чужой причиной
    # (ломало clear-skipped --reason и отчётность). Дефолт сохраняет прежнее
    # поведение questions-пути (#95); already-responded-путь передаёт свой explicit.
    skip_reason: str = SKIP_REASONS.HAS_QUESTIONS
    # #163: реальное действие на hh.ru выполнено (submit формы отклика).
    # False у всех выходов до submit (форма входа, «уже откликались», кнопка
    # не найдена, dry-run) — цикл откликов не пишет их в actions и не ждёт
    # throttle.wait. True после submit-клика: даже провал подтверждения
    # успеха (wait_success_confirmation) — отправка была, пауза обязательна.
    acted: bool = False
    # #176: отправка могла уйти, но результат неизвестен — Playwright бросил
    # исключение во время/сразу после submit-клика. fail-closed: acted=True +
    # uncertain=True, чтобы цикл откликов писал action со статусом 'uncertain'
    # (дедупликация has_applied его видит — без этого возможен повторный
    # отклик на ту же вакансию) и выдерживал троттл-паузу.
    uncertain: bool = False
    stop_run: bool = False
    outcome_code: str = ""

    def __post_init__(self) -> None:
        if self.outcome_code:
            return
        self.outcome_code = (
            "skipped"
            if self.skipped
            else "uncertain"
            if self.uncertain
            else "success"
            if self.success
            else "failed"
        )


@dataclass
class ApplyContext:
    """Контекст одного отклика. Пробрасывается в шаги; probe — хук #8."""

    page: Page
    vacancy: VacancyCard
    resume_id: str
    cover_letter_template: str
    dry_run: bool
    probe: ProbeHook = field(default_factory=lambda: NOOP_PROBE)
    # #17: провайдер письма (шаблон/AI). None → статичный .format (обратная
    # совместимость). Провайдер сам отвечает за fallback, исключений не кидает.
    letter_provider: CoverLetterProvider | None = None
    # Заполняется в _run после рендера письма — итоговый variant для ApplyResult.
    letter_variant: str = VARIANT_TEMPLATE
    # #163: submit выполнен. Выставляется в _run после успешного fill_response_form
    # (клик по кнопке отправки — его последний шаг); все ПОСЛЕДУЮЩИЕ исходы
    # (успех, провал подтверждения) несут acted=True через fail()/ok().
    acted: bool = False
    # #176: как в ApplyResult — выставляется в _run при SubmitClickUncertain.
    uncertain: bool = False
    # #207: внешний верификатор fail-вердиктов после клика по кнопке отклика
    # (см. _finalize_post_click_failure). None — проверка выключена: юнит-тесты
    # pipeline инъектируют фейк или ничего; продакшн-проводка — в
    # commands/_common.run_apply_for_resume (verify_response_in_negotiations).
    verifier: ResponseVerifier | None = None
    # #245: durable audit marker immediately before entering the submit path.
    before_submit: Callable[[], None] | None = None
    question_answerer: AIQuestionAnswerer | None = None
    questionnaire_history: History | None = None
    run_id: str | None = None
    force: bool = False
    allow_relocation: bool = False

    def fail(self, reason: str) -> ApplyResult:
        return ApplyResult(
            self.vacancy,
            False,
            reason,
            letter_variant=self.letter_variant,
            acted=self.acted,
            uncertain=self.uncertain,
        )

    def stop(self, reason: str) -> ApplyResult:
        return ApplyResult(
            self.vacancy,
            False,
            reason,
            letter_variant=self.letter_variant,
            acted=self.acted,
            uncertain=self.uncertain,
            stop_run=True,
        )

    def ok(self, reason: str, *, outcome_code: str = "success") -> ApplyResult:
        return ApplyResult(
            self.vacancy,
            True,
            reason,
            letter_variant=self.letter_variant,
            acted=self.acted,
            outcome_code=outcome_code,
        )

    def skip(self, reason: str, skip_reason: str = SKIP_REASONS.HAS_QUESTIONS) -> ApplyResult:
        # #95: skip отличён от fail — отправки не было, но и ошибки нет. success=False,
        # skipped=True: цикл откликов пишет record_skip (НЕ record_action failed) и
        # не ждёт throttle. mirror of fail()/ok() — несёт letter_variant для консистентности.
        # #163: acted всегда False — skip по определению до submit.
        # #226: skip_reason — persistent причина для record_skip, дефолт совпадает
        # с прежним единственным вызывающим (#95 questions).
        return ApplyResult(
            self.vacancy,
            success=False,
            reason=reason,
            letter_variant=self.letter_variant,
            skipped=True,
            skip_reason=skip_reason,
            outcome_code="skipped",
        )


def apply_to_vacancy(
    page: Page,
    vacancy: VacancyCard,
    resume_id: str,
    cover_letter_template: str,
    dry_run: bool,
    probe: ProbeHook | None = None,
    letter_provider: CoverLetterProvider | None = None,
    verifier: ResponseVerifier | None = None,
    before_submit: Callable[[], None] | None = None,
    question_answerer: AIQuestionAnswerer | None = None,
    questionnaire_history: History | None = None,
    run_id: str | None = None,
    force: bool = False,
    allow_relocation: bool = False,
) -> ApplyResult:
    ctx = ApplyContext(
        page=page,
        vacancy=vacancy,
        resume_id=resume_id,
        cover_letter_template=cover_letter_template,
        dry_run=dry_run,
        probe=probe or NOOP_PROBE,
        letter_provider=letter_provider,
        verifier=verifier,
        before_submit=before_submit,
        question_answerer=question_answerer,
        questionnaire_history=questionnaire_history,
        run_id=run_id,
        force=force,
        allow_relocation=allow_relocation,
    )
    return _run(ctx)


def _record_questionnaire_answers(
    ctx: ApplyContext, proposals: list[AnswerProposal], *, filled: bool
) -> bool:
    """Persist a local audit snapshot without claiming the response was sent."""
    if ctx.questionnaire_history is None or ctx.dry_run:
        return True
    try:
        ctx.questionnaire_history.record_questionnaire(
            ctx.resume_id,
            ctx.vacancy.vacancy_id,
            ctx.vacancy.url,
            ctx.vacancy.title,
            ctx.vacancy.company,
            [
                {
                    "body_index": proposal.question.body_index,
                    "text": proposal.question.text,
                    "kind": proposal.question.kind,
                    "is_radio": proposal.question.is_radio,
                    "options": list(proposal.question.options),
                    # A low-confidence proposal deliberately records no
                    # answer, even if malformed model output contained one.
                    "answer": proposal.answer if not proposal.low_confidence else "",
                    "answer_source": proposal.answer_source,
                    "confidence": proposal.confidence,
                    "filled": filled,
                    # #482: attribute the row to a learnable template (when
                    # the resolver answered it) and a deterministic cluster
                    # key (always -- lets stats group repeat questions across
                    # vacancies the same way apply/questionnaire.py's bulk
                    # scan report does).
                    "template": proposal.template,
                    "cluster": question_cluster_key(proposal.question),
                }
                for proposal in proposals
            ],
            source="apply",
            run_id=ctx.run_id,
        )
        return True
    except sqlite3.Error as exc:
        logger.warning("[FAIL] %s — не удалось записать аудит анкеты: %s", ctx.vacancy.title, exc)
        return False


def _finalize_blocker(ctx: ApplyContext, blocker: PostClickBlocker) -> ApplyResult:
    """Финализирует терминальный post-click блокер (#342) с учётом #207.

    Блокер, найденный ДО навигации на форму, отклик отправить не мог — его
    вердикт терминален сам по себе (как #350). Блокер, найденный ПОСЛЕ
    навигации, попадает в «серую зону» #207: hh.ru мог принять отклик и
    одновременно показать модалку, поэтому сначала спрашиваем внешний источник
    истины. Отклик найден — это success, а не потерянный skip; иначе вердикт
    блокера сохраняется.
    """

    def _verdict() -> ApplyResult:
        if blocker.stop_run:
            return ctx.stop(blocker.reason)
        return ctx.skip(
            blocker.reason,
            skip_reason=blocker.skip_reason or SKIP_REASONS.RESPONSE_REJECTED,
        )

    def _keep_stop(result: ApplyResult) -> ApplyResult:
        """Сохраняет остановку прогона независимо от вердикта проверки.

        Лимит откликов аккаунта — свойство аккаунта, а не конкретной вакансии:
        подтверждённый отклик (found) или неудавшаяся проверка не отменяют его.
        Без этого прогон продолжил бы долбиться в исчерпанный лимит — ровно тот
        сценарий, ради которого #342 и заводился.
        """
        if blocker.stop_run and not result.stop_run:
            return replace(result, stop_run=True)
        return result

    if not blocker.post_navigation or ctx.dry_run or ctx.verifier is None:
        return _verdict()
    try:
        verified = ctx.verifier(ctx.page, ctx.vacancy.vacancy_id, ctx.resume_id)
    except AntiBotChallengeDetected:
        raise
    except Exception as exc:  # noqa: BLE001
        # Как и в _finalize_post_click_failure: сбой самой проверки — это
        # «не смогли проверить», а не «отклика нет». Fail-closed.
        ctx.acted = True
        ctx.uncertain = True
        logger.warning("%s — внешняя проверка упала: %s", ctx.vacancy.title, exc)
        return _keep_stop(
            ctx.fail(f"{blocker.reason}; внешняя проверка упала ({exc}) — исход неопределён")
        )
    if verified.found:
        ctx.acted = True
        ctx.uncertain = False
        logger.info(
            "[OK] %s — блокер показан, но внешний источник подтвердил отклик: %s",
            ctx.vacancy.title,
            verified.detail,
        )
        return _keep_stop(
            ctx.ok(
                f"{blocker.reason}; внешняя проверка: отклик присутствует "
                f"в /applicant/negotiations ({verified.detail})"
            )
        )
    if verified.indeterminate:
        ctx.acted = True
        ctx.uncertain = True
        logger.warning("%s — внешняя проверка недоступна: %s", ctx.vacancy.title, verified.detail)
        return _keep_stop(
            ctx.fail(
                f"{blocker.reason}; внешняя проверка недоступна ({verified.detail}) — "
                "исход неопределён"
            )
        )
    return _verdict()


def _finalize_post_click_failure(ctx: ApplyContext, reason: str) -> ApplyResult:
    """#207: финализация fail-вердикта ПОСЛЕ клика по кнопке отклика.

    Кнопка отклика открывает «серую зону»: дальнейшие таймауты (навигация к
    форме, отрисовка, success-сигнал) не доказывают отсутствие отклика —
    известны кейсы, когда отклик уходил на hh.ru при полном провале пайплайна
    (#199/МТС: письмо работодателя; #207/YADRO: карточка в negotiations). До
    финализации спрашиваем внешний источник истины — /applicant/negotiations:

    * found — отклик точно ушёл: success, acted=True, uncertain сброшен;
    * not_found — список подтверждённо прочитан и вакансии нет: отклик точно НЕ
      ушёл. Вердикт остаётся fail, но uncertain СБРАСЫВАЕТСЯ — симметрично
      ветке found: внешний источник главнее локальной неопределённости.
      Раньше флаги здесь не трогались вовсе, и предустановленный
      SubmitClickUncertain'ом uncertain переживал опровергающую его проверку →
      в actions писался status='uncertain' при доказанном отсутствии отклика
      (боевой случай vacancy_id=136190065, 2026-08-20): он расходовал дневной
      лимит (count_today) и навсегда блокировал вакансию (has_applied);
    * indeterminate — список не прочитан: fail-closed uncertain+acted —
      has_applied видит запись, троттл ждёт (как у #176).
    """
    # cycle-review round 2 (#373): dry-run with an answerer configured still
    # clicks VACANCY_APPLY_BUTTON to preview questions (#97 contract), so it
    # CAN reach this grey-zone finalizer despite never submitting. A dry-run
    # never sends anything: fail-closed here means "not verified", never
    # "acted", regardless of what the external verifier would have said. The
    # command layer also records actions only after a real submit.
    if ctx.dry_run:
        return ctx.fail(reason)
    # Inspect the page that failed before the verifier navigates away. A submit
    # navigation may render a challenge and still raise SubmitClickUncertain.
    _halt_if_antibot(ctx)
    if ctx.verifier is None:
        return ctx.fail(reason)
    try:
        verdict = ctx.verifier(ctx.page, ctx.vacancy.vacancy_id, ctx.resume_id)
    except AntiBotChallengeDetected:
        # A confirmed challenge is terminal, unlike an arbitrary verifier
        # crash. The pre-submit audit reservation remains fail-closed uncertain.
        raise
    except Exception as exc:  # noqa: BLE001
        # #207: сбой самой внешней проверки не должен обрывать apply до записи
        # в history и паузы троттлинга — иначе следующий запуск не увидит запись
        # и отправит дубликат. Fail-closed: uncertain + acted, как у #176.
        # Ловим Exception, а не только PlaywrightError: верификатор читает
        # чужой SSR/DOM, и не-Playwright ошибка парсинга (ValueError/TypeError
        # из parse_response_card/_ssr_topic_list) — тот же класс неопределённости
        # «не смогли проверить», что и упавшая страница. Граница fail-closed —
        # единственное место, где широкий except оправдан: цена ложного
        # uncertain (лишняя пауза) ниже цены пропущенной записи (дубликат).
        ctx.acted = True
        ctx.uncertain = True
        logger.warning("%s — внешняя проверка упала: %s", ctx.vacancy.title, exc)
        return ctx.fail(f"{reason}; внешняя проверка упала ({exc}) — исход неопределён")
    if verdict.found:
        ctx.acted = True
        ctx.uncertain = False
        logger.info(
            "[OK] %s — внешний источник подтвердил отклик: %s",
            ctx.vacancy.title,
            verdict.detail,
        )
        return ctx.ok(
            f"внешняя сверка подтвердила отклик в /applicant/negotiations ({verdict.detail})",
            outcome_code="reconciled_success",
        )
    if verdict.indeterminate:
        ctx.acted = True
        ctx.uncertain = True
        logger.warning("%s — внешняя проверка недоступна: %s", ctx.vacancy.title, verdict.detail)
        return ctx.fail(
            f"{reason}; внешняя проверка недоступна ({verdict.detail}) — исход неопределён"
        )
    # Контракт verify.py: not_found — список ПОДТВЕРЖДЁННО прочитан (SSR распарсен
    # либо карточки отрендерились) и вакансии в нём нет → отклик точно НЕ ушёл
    # (topicList наполняется синхронно с submit). Поэтому неопределённость,
    # выставленную ДО проверки (SubmitClickUncertain / post-submit PlaywrightError),
    # здесь СНИМАЕМ: внешний источник главнее локального «мог уйти» — ровно тем же
    # правом, каким его снимает ветка found выше.
    # #176 не нарушен: он про случай, когда внешней проверки НЕТ вообще —
    # ветка `ctx.verifier is None` возвращает fail раньше, не касаясь флагов.
    # acted НЕ трогаем: клик по кнопке отклика был, пауза троттлинга заслужена.
    # Следствие (желаемое): без uncertain строка пишется как 'failed', has_applied()
    # перестаёт блокировать вакансию и следующий прогон её ретрайнет — это верно,
    # not_found означает, что ничего не отправлялось, и страх дубликата письма
    # из #176 к этому случаю неприменим. Дневной лимит тоже не расходуется впустую.
    ctx.uncertain = False
    logger.info("%s — внешняя проверка: отклика в /applicant/negotiations нет", ctx.vacancy.title)
    return ctx.fail(f"{reason}; внешняя проверка: отклика в /applicant/negotiations нет")


def _run(ctx: ApplyContext) -> ApplyResult:
    logger.info("Открываю вакансию: %s (%s)", ctx.vacancy.title, ctx.vacancy.url)
    goto_hh(ctx.page, ctx.vacancy.url)
    _halt_if_antibot(ctx)
    if has_login_form(ctx.page):
        return ctx.fail("Сессия недействительна: страница содержит форму входа. Выполните login.")
    ctx.probe("vacancy_loaded", url=ctx.vacancy.url)

    apply_button_found = apply_steps.wait_apply_button(ctx.page)
    # A challenge can render asynchronously while wait_apply_button is waiting.
    # Recheck before turning the missing button into an ordinary per-vacancy fail.
    _halt_if_antibot(ctx)
    # The combined wait can observe both markers during a transitional SPA
    # render. Re-check independently after it completes so the apply button
    # cannot win over an already-responded marker. #241 cycle-review round 2
    # rejected a non-blocking fast path here twice (see dedup.py docstring) —
    # the blocking check stays unconditional.
    if reason := check_already_responded(ctx.page, ctx.vacancy):
        return ctx.skip(reason, skip_reason=SKIP_REASONS.ALREADY_APPLIED)
    if not apply_button_found:
        return ctx.fail("кнопка отклика не найдена на странице")

    # #17: рендер письма через провайдер, если он задан (AI/шаблон). Провайдер
    # сам падает на шаблон при сбое — исключений не ждём. variant фиксируем в
    # контексте, чтобы ApplyResult понёс его в history (A/B-срез, Этап 3).
    if ctx.letter_provider is not None:
        outcome = ctx.letter_provider.render(ctx.vacancy)
        letter = outcome.text
        ctx.letter_variant = outcome.variant
    else:
        letter = render_cover_letter(ctx.cover_letter_template, ctx.vacancy)
        ctx.letter_variant = VARIANT_TEMPLATE

    if ctx.dry_run:
        logger.info("[DRY-RUN] Откликнулся бы на '%s' с письмом:\n%s", ctx.vacancy.title, letter)
    if ctx.dry_run and ctx.question_answerer is None:
        return ctx.ok("dry-run")

    # #247: ревалидация маркера «уже откликались» ДО клика по кнопке отклика.
    # Маркеры — vacancy-page селекторы (dedup.py), их нет в DOM формы
    # /applicant/vacancy_response, поэтому проверка после navigate_to_response_form
    # была бы неэффективной. Здесь, на странице вакансии, она ловит маркер,
    # отрендерившийся за время рендера письма (letter render — самое долгое
    # окно TOCTOU, особенно с AI-провайдером). Окно самой навигации (после
    # клика) vacancy-маркерами не покрыть — его компенсирует post-click
    # верификация #207 (_finalize_post_click_failure).
    if reason := check_already_responded(ctx.page, ctx.vacancy):
        return ctx.skip(reason, skip_reason=SKIP_REASONS.ALREADY_APPLIED)

    # #207: с клика по кнопке отклика начинается «серая зона» — дальнейшие
    # fail-исходы финализируются через _finalize_post_click_failure (внешняя
    # проверка /applicant/negotiations), а не сразу ctx.fail.
    navigation_result = apply_steps.navigate_to_response_form(
        ctx.page,
        ctx.vacancy.vacancy_id,
        allow_relocation=ctx.allow_relocation,
    )
    _halt_if_antibot(ctx)
    if isinstance(navigation_result, str):
        # #350: развёрнутое предупреждение о видимости резюме — недвусмысленный,
        # неисполнимый пропуск; не форма не отрисовалась, а hh.ru дал определённый
        # ответ прямо на странице вакансии.
        return ctx.skip(navigation_result, skip_reason=SKIP_REASONS.RESUME_VISIBILITY)
    if isinstance(navigation_result, PostClickBlocker):
        return _finalize_blocker(ctx, navigation_result)
    if not navigation_result:
        reason = "форма отклика не отрисовалась — состояние формы не подтверждено"
        logger.warning("[FAIL] %s — %s", ctx.vacancy.title, reason)
        return _finalize_post_click_failure(ctx, reason)
    ctx.probe("form_loaded")

    # #95/#97: detect questions before any form write. #97 is opt-in and keeps
    # the old detect-only skip path when no answerer is configured.
    questions = detect_questions(ctx.page)
    if questions.indeterminate:
        # round-2 fix: границы формы не резолвились — блокируем отправку, но НЕ
        # пишем persistent skip (fail, не skip): недостоверная причина не должна
        # навсегда исключать вакансию из is_skipped (#87).
        logger.warning("[FAIL] %s — %s", ctx.vacancy.title, questions.reason)
        return _finalize_post_click_failure(ctx, questions.reason)
    if questions.has_questions and ctx.question_answerer is None:
        logger.info("[skip] %s — %s", ctx.vacancy.title, questions.reason)
        return ctx.skip(questions.reason)
    # M6 cycle-review #373: gate --force on questions actually found in THIS
    # vacancy, not on the answerer merely being configured. The old gate lived
    # before detect_questions() (and a duplicate copy in _common.py fired
    # before the per-vacancy loop even started) — both blocked an entire
    # `apply` run whenever ai.answer_questions: true was set, even for
    # vacancies with no questionnaire at all. real submit of a questionnaire
    # still requires explicit --force.
    needs_force = questions.has_questions and ctx.question_answerer is not None
    if needs_force and not ctx.dry_run and not ctx.force:
        return ctx.fail("LLM-ответы на вопросы требуют явного --force")
    if ctx.question_answerer is not None:
        try:
            if questions.has_questions:
                extracted, total_bodies = extract_questions(ctx.page)
            else:
                extracted, total_bodies = [], 0
        except PlaywrightError as exc:
            # M5 cycle-review #373: extract_questions() re-reads live DOM after
            # a React render (navigate_to_response_form only guarantees
            # wait_until="commit" — see CLAUDE.md) and previously ran outside
            # any try/except here; a raw PlaywrightError would abort the whole
            # apply loop for the remaining vacancies/resumes with a traceback,
            # same class of bug as fill_response_form below (#163).
            reason = f"ошибка Playwright при извлечении вопросов анкеты ({exc})"
            logger.warning("[FAIL] %s — %s", ctx.vacancy.title, reason)
            return ctx.fail(reason)
        # cycle-review #373 (B1) + codex review round 2 (P1): detect_questions()
        # has_questions=True can come from either the confirmed task-body path
        # or the unconfirmed heuristic fallback (radio/checkbox/textarea
        # without task-body — see apply/questions.py). extract_questions()
        # only recognises the task-body structure, and even within that
        # structure a specific body can be dropped (unrecognisable
        # radio/checkbox layout, or blank/duplicate option labels — M7).
        # A bare `not extracted` check only catches a FULLY empty result — a
        # PARTIAL mismatch (e.g. 2 bodies detected, 1 parses, 1 dropped) would
        # leave `extracted` non-empty and slip through, submitting the form
        # with one question silently unanswered. Comparing counts catches
        # both: the heuristic-only case (extracted=[], total_bodies=0, still
        # a mismatch against questions.has_questions) and any partial drop.
        # "Wrong/skipped answer is safer than a silent blank submit" (#97):
        # treat this the same as the no-answerer skip path.
        if questions.has_questions and (not extracted or len(extracted) != total_bodies):
            reason = (
                "анкета обнаружена, но не распознана полностью для LLM-ответа "
                "(расхождение эвристики и парсера вопросов)"
            )
            logger.warning("[skip] %s — %s", ctx.vacancy.title, reason)
            return ctx.skip(reason, skip_reason=SKIP_REASONS.HAS_QUESTIONS)
        proposals = ctx.question_answerer.propose_all(extracted)
        prefix = "[DRY-RUN]" if ctx.dry_run else "[FORCE]"
        for proposal in proposals:
            logger.info(
                "%s Вопрос: %s | Ответ: %s | confidence=%.2f",
                prefix,
                proposal.question.text,
                proposal.answer or "(пропущен: низкая уверенность)",
                proposal.confidence,
            )
        low_confidence = [proposal for proposal in proposals if proposal.low_confidence]
        if low_confidence:
            if not _record_questionnaire_answers(ctx, proposals, filled=False):
                return ctx.fail("не удалось записать аудит ответов анкеты")
            reason = (
                f"пропущен вопрос с низкой уверенностью ({len(low_confidence)}): "
                + low_confidence[0].question.text
            )
            # #87 cycle-review: LLM confidence is non-deterministic between
            # runs (unlike stopword/exclude filters), so a dry-run preview
            # must not permanently bury the vacancy via record_skip — same
            # reasoning as the indeterminate-scope path above (line ~301),
            # which uses fail() rather than skip() for the same reason.
            # ctx.skip() always persists via _common.py's unconditional
            # record_skip(result.skip_reason), so only a real (non-dry-run)
            # low-confidence outcome may use it.
            if ctx.dry_run:
                return ctx.fail(reason + " (dry-run — предпросмотр, не сохраняется)")
            return ctx.skip(reason, skip_reason=SKIP_REASONS.QUESTION_LOW_CONFIDENCE)
        if ctx.dry_run:
            return ctx.ok("dry-run: предложенные ответы на вопросы показаны")
        try:
            ctx.question_answerer.apply(ctx.page, proposals)
        except PlaywrightError as exc:
            # M5 cycle-review #373: same reasoning as extract_questions() above
            # — this happens before the submit click (acted stays False), so a
            # clean fail here (no on-site trace) matches the fill_response_form
            # PlaywrightError branch below rather than treating it as uncertain.
            reason = f"ошибка Playwright при заполнении ответов на вопросы ({exc})"
            logger.warning("[FAIL] %s — %s", ctx.vacancy.title, reason)
            return ctx.fail(reason)
        if not _record_questionnaire_answers(ctx, proposals, filled=True):
            return ctx.fail("не удалось записать аудит заполненной анкеты")

    # #176: окно действия. Submit-клик — единственный необратимый шаг формы;
    # исключение в момент/сразу после него (navigation timeout после POST,
    # target closed) не означает, что отклик НЕ ушёл. Трактовка fail-closed:
    #   * SubmitClickUncertain (клик мог уйти) — acted+uncertain: запись
    #     'uncertain' в actions (дедупликация его видит) + троттл-пауза;
    #   * прочий PlaywrightError из заполнения (до submit: toggle/fill) —
    #     чистый fail с acted=False: на hh.ru следа нет, как у остальных
    #     ранних выходов #163, но traceback больше не рвёт цикл откликов.
    try:
        # Last pre-submit barrier: a challenge rendered after the form checks
        # terminates the whole command before the irreversible click/audit marker.
        _halt_if_antibot(ctx)
        if ctx.before_submit is not None:
            ctx.before_submit()
        reason = apply_steps.fill_response_form(ctx.page, ctx.resume_id, letter)
    except SubmitClickUncertain:
        ctx.acted = True
        ctx.uncertain = True
        logger.warning(
            "[FAIL] %s — submit-клик упал с исключением, отправка могла уйти",
            ctx.vacancy.title,
        )
        return _finalize_post_click_failure(
            ctx, "submit-клик упал с исключением — отправка могла уйти, исход неопределён"
        )
    except PostSubmitLimitExceeded as exc:
        ctx.acted = True
        return ctx.stop(str(exc))
    except PlaywrightError as exc:
        logger.warning(
            "[FAIL] %s — ошибка Playwright при заполнении формы (%s)", ctx.vacancy.title, exc
        )
        return _finalize_post_click_failure(
            ctx, f"ошибка Playwright при заполнении формы отклика: {exc}"
        )
    if reason:
        return _finalize_post_click_failure(ctx, reason)

    # #163: fill_response_form без причины = submit-клик выполнен (последний шаг
    # заполнения). Дальнейшие исходы — «после действия»: пауза и запись в actions.
    ctx.acted = True

    # #176: подтверждение успеха — тоже «после действия»: исключение здесь
    # (а не только False) обязано вернуться fail-результатом с acted=True,
    # иначе цикл упадёт трейсбеком и отправка не попадёт в history/троттл.
    try:
        ctx.probe("submitted", vacancy_title=ctx.vacancy.title)

        if not wait_success_confirmation(ctx.page, terminal_check=lambda: _halt_if_antibot(ctx)):
            # Честный union-poll до таймаута БЕЗ сигнала — локально успеха не
            # нашли (#163). Но это всё ещё «после действия»: вердикт финализируется
            # внешней проверкой (#207) — found → success, неопределённость чтения
            # → uncertain. Подтверждённое отсутствие — остаётся failed.
            return _finalize_post_click_failure(
                ctx, "не удалось подтвердить успешную отправку отклика"
            )
    except PlaywrightError as exc:
        # #177 round 3 (Codex): исключение — НЕ то же самое, что False выше.
        # Мы не смогли даже проверить (browser/page упал посреди опроса),
        # а не «проверили и не нашли» — тот же класс неопределённости, что
        # и SubmitClickUncertain при самом клике. uncertain=True обязателен,
        # иначе has_applied() не отсечёт вакансию и уйдёт второй отклик.
        ctx.uncertain = True
        logger.warning(
            "[FAIL] %s — ошибка Playwright после отправки (%s), успех не подтверждён",
            ctx.vacancy.title,
            exc,
        )
        return _finalize_post_click_failure(
            ctx, f"ошибка Playwright после отправки отклика ({exc}) — успех не подтверждён"
        )

    logger.info("Отклик отправлен: %s", ctx.vacancy.title)
    return ctx.ok("success")


def _halt_if_antibot(ctx: ApplyContext) -> None:
    """Raise the terminal signal without creating a per-vacancy action record."""

    detection = detect_antibot_on_page(ctx.page)
    if detection is None:
        return
    logger.error("[FAIL] %s — %s", ctx.vacancy.title, detection.detail)
    raise AntiBotChallengeDetected(detection)

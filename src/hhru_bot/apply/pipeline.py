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
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..browser import goto_hh, has_login_form
from ..history import SKIP_REASONS
from ..search import VacancyCard
from . import steps as apply_steps
from .dedup import check_already_responded
from .letter import VARIANT_TEMPLATE, CoverLetterProvider, render_cover_letter
from .probe import NOOP_PROBE, ProbeHook
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

    def fail(self, reason: str) -> ApplyResult:
        return ApplyResult(
            self.vacancy,
            False,
            reason,
            letter_variant=self.letter_variant,
            acted=self.acted,
            uncertain=self.uncertain,
        )

    def ok(self, reason: str) -> ApplyResult:
        return ApplyResult(
            self.vacancy,
            True,
            reason,
            letter_variant=self.letter_variant,
            acted=self.acted,
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
    )
    return _run(ctx)


def _finalize_post_click_failure(ctx: ApplyContext, reason: str) -> ApplyResult:
    """#207: финализация fail-вердикта ПОСЛЕ клика по кнопке отклика.

    Кнопка отклика открывает «серую зону»: дальнейшие таймауты (навигация к
    форме, отрисовка, success-сигнал) не доказывают отсутствие отклика —
    известны кейсы, когда отклик уходил на hh.ru при полном провале пайплайна
    (#199/МТС: письмо работодателя; #207/YADRO: карточка в negotiations). До
    финализации спрашиваем внешний источник истины — /applicant/negotiations:

    * found — отклик точно ушёл: success, acted=True, uncertain сброшен;
    * not_found — список подтверждённо прочитан и вакансии нет: вердикт без
      изменений (post-submit → failed, ранние выходы → без записи; флаги,
      выставленные сайтом ДО проверки — например uncertain у
      SubmitClickUncertain, — сохраняются);
    * indeterminate — список не прочитан: fail-closed uncertain+acted —
      has_applied видит запись, троттл ждёт (как у #176).
    """
    if ctx.verifier is None:
        return ctx.fail(reason)
    try:
        verdict = ctx.verifier(ctx.page, ctx.vacancy.vacancy_id, ctx.resume_id)
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
            f"{reason}; внешняя проверка: отклик присутствует в /applicant/negotiations "
            f"({verdict.detail})"
        )
    if verdict.indeterminate:
        ctx.acted = True
        ctx.uncertain = True
        logger.warning("%s — внешняя проверка недоступна: %s", ctx.vacancy.title, verdict.detail)
        return ctx.fail(
            f"{reason}; внешняя проверка недоступна ({verdict.detail}) — исход неопределён"
        )
    logger.info("%s — внешняя проверка: отклика в /applicant/negotiations нет", ctx.vacancy.title)
    return ctx.fail(f"{reason}; внешняя проверка: отклика в /applicant/negotiations нет")


def _run(ctx: ApplyContext) -> ApplyResult:
    logger.info("Открываю вакансию: %s (%s)", ctx.vacancy.title, ctx.vacancy.url)
    goto_hh(ctx.page, ctx.vacancy.url)
    if has_login_form(ctx.page):
        return ctx.fail("Сессия недействительна: страница содержит форму входа. Выполните login.")
    ctx.probe("vacancy_loaded", url=ctx.vacancy.url)

    apply_button_found = apply_steps.wait_apply_button(ctx.page)
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
    apply_steps.navigate_to_response_form(ctx.page, ctx.vacancy.vacancy_id)
    ctx.probe("form_loaded")

    # #95: detect-only проверка на вопросы/анкету. Делается ДО fill_response_form:
    # форма с вопросами НЕ заполняется и НЕ отправляется (fail-closed по submit).
    questions = detect_questions(ctx.page)
    if questions.indeterminate:
        # round-2 fix: границы формы не резолвились — блокируем отправку, но НЕ
        # пишем persistent skip (fail, не skip): недостоверная причина не должна
        # навсегда исключать вакансию из is_skipped (#87).
        logger.warning("[FAIL] %s — %s", ctx.vacancy.title, questions.reason)
        return _finalize_post_click_failure(ctx, questions.reason)
    if questions.has_questions:
        logger.info("[skip] %s — %s", ctx.vacancy.title, questions.reason)
        return ctx.skip(questions.reason)

    # #176: окно действия. Submit-клик — единственный необратимый шаг формы;
    # исключение в момент/сразу после него (navigation timeout после POST,
    # target closed) не означает, что отклик НЕ ушёл. Трактовка fail-closed:
    #   * SubmitClickUncertain (клик мог уйти) — acted+uncertain: запись
    #     'uncertain' в actions (дедупликация его видит) + троттл-пауза;
    #   * прочий PlaywrightError из заполнения (до submit: toggle/fill) —
    #     чистый fail с acted=False: на hh.ru следа нет, как у остальных
    #     ранних выходов #163, но traceback больше не рвёт цикл откликов.
    try:
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

        if not wait_success_confirmation(ctx.page):
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

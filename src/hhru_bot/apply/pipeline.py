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
from ..search import VacancyCard
from . import steps as apply_steps
from .dedup import check_already_responded
from .letter import VARIANT_TEMPLATE, CoverLetterProvider, render_cover_letter
from .probe import NOOP_PROBE, ProbeHook
from .questions import detect_questions
from .steps import SubmitClickUncertain
from .success import wait_success_confirmation

logger = logging.getLogger("hhru_bot.apply")


@dataclass
class ApplyResult:
    vacancy: VacancyCard
    success: bool
    reason: str = ""
    # A/B-вариант письма (#17): 'template' / 'ai' / 'ai_fallback'. Для записи в
    # history.actions.letter_variant. По умолчанию 'template' (без AI-провайдера).
    letter_variant: str = VARIANT_TEMPLATE
    # #95: skip — третий исход (помимо success/fail). True → вакансия требует анкеты,
    # submit НЕ выполнялся. В отличие от fail, skip не пишет status='failed' в actions
    # и не расходует дневной лимит/троттл (см. commands/_common.run_apply_for_resume).
    skipped: bool = False
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

    def skip(self, reason: str) -> ApplyResult:
        # #95: skip отличён от fail — отправки не было, но и ошибки нет. success=False,
        # skipped=True: цикл откликов пишет record_skip (НЕ record_action failed) и
        # не ждёт throttle. mirror of fail()/ok() — несёт letter_variant для консистентности.
        # #163: acted всегда False — skip по определению до submit.
        return ApplyResult(
            self.vacancy,
            success=False,
            reason=reason,
            letter_variant=self.letter_variant,
            skipped=True,
        )


def apply_to_vacancy(
    page: Page,
    vacancy: VacancyCard,
    resume_id: str,
    cover_letter_template: str,
    dry_run: bool,
    probe: ProbeHook | None = None,
    letter_provider: CoverLetterProvider | None = None,
) -> ApplyResult:
    ctx = ApplyContext(
        page=page,
        vacancy=vacancy,
        resume_id=resume_id,
        cover_letter_template=cover_letter_template,
        dry_run=dry_run,
        probe=probe or NOOP_PROBE,
        letter_provider=letter_provider,
    )
    return _run(ctx)


def _run(ctx: ApplyContext) -> ApplyResult:
    logger.info("Открываю вакансию: %s (%s)", ctx.vacancy.title, ctx.vacancy.url)
    goto_hh(ctx.page, ctx.vacancy.url)
    if has_login_form(ctx.page):
        return ctx.fail("Сессия недействительна: страница содержит форму входа. Выполните login.")
    ctx.probe("vacancy_loaded", url=ctx.vacancy.url)

    if reason := check_already_responded(ctx.page, ctx.vacancy):
        return ctx.fail(reason)

    if not apply_steps.wait_apply_button(ctx.page):
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

    apply_steps.navigate_to_response_form(ctx.page)
    ctx.probe("form_loaded")

    # #95: detect-only проверка на вопросы/анкету. Делается ДО fill_response_form:
    # форма с вопросами НЕ заполняется и НЕ отправляется (fail-closed по submit).
    questions = detect_questions(ctx.page)
    if questions.indeterminate:
        # round-2 fix: границы формы не резолвились — блокируем отправку, но НЕ
        # пишем persistent skip (fail, не skip): недостоверная причина не должна
        # навсегда исключать вакансию из is_skipped (#87).
        logger.warning("[FAIL] %s — %s", ctx.vacancy.title, questions.reason)
        return ctx.fail(questions.reason)
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
        return ctx.fail("submit-клик упал с исключением — отправка могла уйти, исход неопределён")
    except PlaywrightError as exc:
        logger.warning(
            "[FAIL] %s — ошибка Playwright при заполнении формы (%s)", ctx.vacancy.title, exc
        )
        return ctx.fail(f"ошибка Playwright при заполнении формы отклика: {exc}")
    if reason:
        return ctx.fail(reason)

    # #163: fill_response_form без причины = submit-клик выполнен (последний шаг
    # заполнения). Дальнейшие исходы — «после действия»: пауза и запись в actions.
    ctx.acted = True

    # #176: подтверждение успеха — тоже «после действия»: исключение здесь
    # (а не только False) обязано вернуться fail-результатом с acted=True,
    # иначе цикл упадёт трейсбеком и отправка не попадёт в history/троттл.
    try:
        ctx.probe("submitted", vacancy_title=ctx.vacancy.title)

        if not wait_success_confirmation(ctx.page):
            # Честный union-poll до таймаута БЕЗ сигнала — достоверно
            # проверили и не нашли успеха, осознанный fail-closed #163
            # (wait_success_confirmation docstring): uncertain НЕ ставим.
            return ctx.fail("не удалось подтвердить успешную отправку отклика")
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
        return ctx.fail(f"ошибка Playwright после отправки отклика ({exc}) — успех не подтверждён")

    logger.info("Отклик отправлен: %s", ctx.vacancy.title)
    return ctx.ok("success")

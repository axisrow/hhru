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

from playwright.sync_api import Page

from ..browser import goto_hh
from ..search import VacancyCard
from . import steps as apply_steps
from .dedup import check_already_responded
from .letter import VARIANT_TEMPLATE, CoverLetterProvider, render_cover_letter
from .probe import NOOP_PROBE, ProbeHook
from .questions import detect_questions
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

    def fail(self, reason: str) -> ApplyResult:
        return ApplyResult(self.vacancy, False, reason, letter_variant=self.letter_variant)

    def ok(self, reason: str) -> ApplyResult:
        return ApplyResult(self.vacancy, True, reason, letter_variant=self.letter_variant)

    def skip(self, reason: str) -> ApplyResult:
        # #95: skip отличён от fail — отправки не было, но и ошибки нет. success=False,
        # skipped=True: цикл откликов пишет record_skip (НЕ record_action failed) и
        # не ждёт throttle. mirror of fail()/ok() — несёт letter_variant для консистентности.
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
    if questions.has_questions:
        logger.info("[skip] %s — %s", ctx.vacancy.title, questions.reason)
        return ctx.skip(questions.reason)

    if reason := apply_steps.fill_response_form(ctx.page, ctx.resume_id, letter):
        return ctx.fail(reason)

    ctx.probe("submitted", vacancy_title=ctx.vacancy.title)

    if not wait_success_confirmation(ctx.page):
        return ctx.fail("не удалось подтвердить успешную отправку отклика")

    logger.info("Отклик отправлен: %s", ctx.vacancy.title)
    return ctx.ok("success")

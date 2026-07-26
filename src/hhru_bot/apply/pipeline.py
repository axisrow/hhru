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

from ..search import VacancyCard
from . import steps as apply_steps
from .dedup import check_already_responded
from .letter import render_cover_letter
from .probe import NOOP_PROBE, ProbeHook
from .success import wait_success_confirmation

logger = logging.getLogger("hhru_bot.apply")


@dataclass
class ApplyResult:
    vacancy: VacancyCard
    success: bool
    reason: str = ""


@dataclass
class ApplyContext:
    """Контекст одного отклика. Пробрасывается в шаги; probe — хук #8."""

    page: Page
    vacancy: VacancyCard
    resume_id: str
    cover_letter_template: str
    dry_run: bool
    probe: ProbeHook = field(default_factory=lambda: NOOP_PROBE)

    def fail(self, reason: str) -> ApplyResult:
        return ApplyResult(self.vacancy, False, reason)

    def ok(self, reason: str) -> ApplyResult:
        return ApplyResult(self.vacancy, True, reason)


def apply_to_vacancy(
    page: Page,
    vacancy: VacancyCard,
    resume_id: str,
    cover_letter_template: str,
    dry_run: bool,
    probe: ProbeHook | None = None,
) -> ApplyResult:
    ctx = ApplyContext(
        page=page,
        vacancy=vacancy,
        resume_id=resume_id,
        cover_letter_template=cover_letter_template,
        dry_run=dry_run,
        probe=probe or NOOP_PROBE,
    )
    return _run(ctx)


def _run(ctx: ApplyContext) -> ApplyResult:
    logger.info("Открываю вакансию: %s (%s)", ctx.vacancy.title, ctx.vacancy.url)
    ctx.page.goto(ctx.vacancy.url, wait_until="domcontentloaded")
    ctx.probe("vacancy_loaded", url=ctx.vacancy.url)

    if reason := check_already_responded(ctx.page, ctx.vacancy):
        return ctx.fail(reason)

    if not apply_steps.wait_apply_button(ctx.page):
        return ctx.fail("кнопка отклика не найдена на странице")

    letter = render_cover_letter(ctx.cover_letter_template, ctx.vacancy)

    if ctx.dry_run:
        logger.info("[DRY-RUN] Откликнулся бы на '%s' с письмом:\n%s", ctx.vacancy.title, letter)
        return ctx.ok("dry-run")

    apply_steps.navigate_to_response_form(ctx.page)
    ctx.probe("form_loaded")

    if reason := apply_steps.fill_response_form(ctx.page, ctx.resume_id, letter):
        return ctx.fail(reason)

    ctx.probe("submitted", vacancy_title=ctx.vacancy.title)

    if not wait_success_confirmation(ctx.page):
        return ctx.fail("не удалось подтвердить успешную отправку отклика")

    logger.info("Отклик отправлен: %s", ctx.vacancy.title)
    return ctx.ok("success")

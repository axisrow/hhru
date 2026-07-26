from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from . import selectors as sel
from .search import VacancyCard

logger = logging.getLogger("hhru_bot.apply")

APPLY_TIMEOUT_MS = 10_000


@dataclass
class ApplyResult:
    vacancy: VacancyCard
    success: bool
    reason: str = ""


def render_cover_letter(template: str, vacancy: VacancyCard) -> str:
    return template.format(vacancy_title=vacancy.title, company_name=vacancy.company)


def apply_to_vacancy(
    page: Page,
    vacancy: VacancyCard,
    resume_id: str,
    cover_letter_template: str,
    dry_run: bool,
) -> ApplyResult:
    logger.info("Открываю вакансию: %s (%s)", vacancy.title, vacancy.url)
    page.goto(vacancy.url, wait_until="domcontentloaded")

    already = page.locator(sel.APPLY_ALREADY_RESPONDED_MARKER)
    if already.count() > 0:
        return ApplyResult(vacancy, False, "уже есть отклик (обнаружено на странице)")

    apply_button = page.locator(sel.VACANCY_APPLY_BUTTON)
    try:
        apply_button.wait_for(timeout=APPLY_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return ApplyResult(vacancy, False, "кнопка отклика не найдена на странице")

    letter = render_cover_letter(cover_letter_template, vacancy)

    if dry_run:
        logger.info(
            "[DRY-RUN] Откликнулся бы на '%s' с письмом:\n%s", vacancy.title, letter
        )
        return ApplyResult(vacancy, True, "dry-run")

    apply_button.click()
    time.sleep(1)

    resume_select = page.locator(sel.APPLY_RESUME_SELECT)
    if resume_select.count() > 0:
        _select_resume_in_form(page, resume_id)

    letter_toggle = page.locator(sel.APPLY_COVER_LETTER_TOGGLE)
    if letter_toggle.count() > 0:
        letter_toggle.click()
        time.sleep(0.5)

    textarea = page.locator(sel.APPLY_COVER_LETTER_TEXTAREA)
    if textarea.count() > 0:
        textarea.fill(letter)
        time.sleep(0.5)

    submit_button = page.locator(sel.APPLY_SUBMIT_BUTTON)
    if submit_button.count() == 0:
        return ApplyResult(vacancy, False, "кнопка отправки отклика не найдена в форме")

    submit_button.click()

    try:
        page.locator(sel.APPLY_SUCCESS_MARKER).wait_for(timeout=APPLY_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return ApplyResult(vacancy, False, "не удалось подтвердить успешную отправку отклика")

    logger.info("Отклик отправлен: %s", vacancy.title)
    return ApplyResult(vacancy, True, "success")


def _select_resume_in_form(page: Page, resume_id: str) -> None:
    """
    Если у пользователя несколько резюме, hh.ru может показать выбор резюме
    в форме отклика. Селектор APPLY_RESUME_SELECT — приблизительный и почти
    наверняка потребует уточнения при первом реальном запуске: нужно найти
    конкретный пункт списка, соответствующий resume_id, и кликнуть на него.
    Пока реализация ищет опцию, содержащую resume_id в data-атрибуте или href.
    """
    options = page.locator(sel.APPLY_RESUME_SELECT)
    count = options.count()
    for i in range(count):
        option = options.nth(i)
        href = option.get_attribute("href") or ""
        if resume_id in href:
            option.click()
            return
    logger.warning(
        "Не удалось однозначно выбрать резюме '%s' в форме отклика — "
        "используется резюме, выбранное hh.ru по умолчанию",
        resume_id,
    )

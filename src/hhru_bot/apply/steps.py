"""Шаги навигации по форме отклика: ожидание кнопки, переход на форму, заполнение.

Владелец: #6. #6 правит wait'ы (таймауты, sleep, явные ожидания) здесь — изолированно
от остальных шагов. Sequence шагов в pipeline.py при этом не меняется.
"""

from __future__ import annotations

import logging
import time

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..selector_groups import vacancy_page

logger = logging.getLogger("hhru_bot.apply.steps")

APPLY_TIMEOUT_MS = 10_000


def wait_apply_button(page: Page) -> bool:
    """Ждёт появления кнопки отклика на странице вакансии. False — не дождались."""
    try:
        page.locator(vacancy_page.VACANCY_APPLY_BUTTON).wait_for(timeout=APPLY_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return False
    return True


def navigate_to_response_form(page: Page) -> None:
    """Кликает кнопку отклика и дожидается навигации на форму отклика.

    VACANCY_APPLY_BUTTON — это <a href="/applicant/vacancy_response?..."> (подтверждено
    curl-дампом реальной страницы вакансии), а не триггер модалки на этой же странице.
    Клик вызывает обычную навигацию — дожидаемся её перед поиском полей формы.
    """
    apply_button = page.locator(vacancy_page.VACANCY_APPLY_BUTTON)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=APPLY_TIMEOUT_MS):
        apply_button.click()
    time.sleep(1)


def fill_response_form(page: Page, resume_id: str, letter: str) -> str | None:
    """Заполняет форму отклика. Возвращает причину отказа или None, если заполнение OK."""
    from ..selector_groups import apply_form

    resume_select = page.locator(apply_form.APPLY_RESUME_SELECT)
    if resume_select.count() > 0:
        _select_resume_in_form(page, resume_id)

    letter_toggle = page.locator(apply_form.APPLY_COVER_LETTER_TOGGLE)
    if letter_toggle.count() > 0:
        letter_toggle.click()
        time.sleep(0.5)

    textarea = page.locator(apply_form.APPLY_COVER_LETTER_TEXTAREA)
    if textarea.count() > 0:
        textarea.fill(letter)
        time.sleep(0.5)

    submit_button = page.locator(apply_form.APPLY_SUBMIT_BUTTON)
    if submit_button.count() == 0:
        return "кнопка отправки отклика не найдена в форме"

    submit_button.click()
    return None


def _select_resume_in_form(page: Page, resume_id: str) -> None:
    """
    Если у пользователя несколько резюме, hh.ru может показать выбор резюме
    в форме отклика. Селектор APPLY_RESUME_SELECT — приблизительный и почти
    наверняка потребует уточнения при первом реальном запуске: нужно найти
    конкретный пункт списка, соответствующий resume_id, и кликнуть на него.
    Пока реализация ищет опцию, содержащую resume_id в data-атрибуте или href.
    """
    from ..selector_groups import apply_form

    options = page.locator(apply_form.APPLY_RESUME_SELECT)
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

"""Шаги навигации по форме отклика: ожидание кнопки, переход на форму, заполнение.

Владелец: #6. #6 правит wait'ы (таймауты, sleep, явные ожидания) здесь — изолированно
от остальных шагов. Sequence шагов в pipeline.py при этом не меняется.

Принцип ожиданий (см. #6): вместо фиксированных time.sleep и проверок count()>0
используются явные ожидания Playwright — locator.wait_for(state=..., timeout=...),
а наличие опционального элемента определяется ловом PlaywrightTimeoutError с коротким
таймаутом. Троттлинг-паузы (анти-бан) сюда не относятся — они в throttle.wait и их
трогать нельзя.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..selector_groups import vacancy_page

logger = logging.getLogger("hhru_bot.apply.steps")

APPLY_TIMEOUT_MS = 10_000
# Короткий таймаут для проверки опциональных полей формы (резюме/письмо могут
# отсутствовать — это нормально, а не ошибка). Ждать полной APPLY_TIMEOUT_MS тут
# бессмысленно: отсутствие поля детерминировано почти сразу.
OPTIONAL_FIELD_TIMEOUT_MS = 1_500


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

    Фиксированный sleep после навигации заменён на явное ожидание готовности DOM:
    ждём любого индикатора формы (кнопка отправки), максимум APPLY_TIMEOUT_MS.
    """
    from ..selector_groups import apply_form

    apply_button = page.locator(vacancy_page.VACANCY_APPLY_BUTTON)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=APPLY_TIMEOUT_MS):
        apply_button.click()
    # Форма рендерится после навигации — ждём её индикатор, а не слепую паузу.
    try:
        page.locator(apply_form.APPLY_SUBMIT_BUTTON).wait_for(
            state="visible", timeout=APPLY_TIMEOUT_MS
        )
    except PlaywrightTimeoutError:
        # Форма не загрузилась — fill_response_form всё равно вернёт причину отказа
        # (submit не найден), логируем для диагностики устаревшего селектора.
        logger.warning("Форма отклика не отрисовалась за %d мс", APPLY_TIMEOUT_MS)


def _is_visible(page: Page, selector: str, *, timeout_ms: int) -> bool:
    """Явное ожидание видимости опционального элемента.

    True — элемент появился и видим; False — не дождались (PlaywrightTimeoutError),
    что для опциональных полей формы означает «на этой странице поля нет».
    Заменяет идиому ``locator.count() > 0``, которая проверяет наличие в DOM без
    гарантии видимости/готовности к взаимодействию.
    """
    try:
        page.locator(selector).wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False
    return True


def fill_response_form(page: Page, resume_id: str, letter: str) -> str | None:
    """Заполняет форму отклика. Возвращает причину отказа или None, если заполнение OK."""
    from ..selector_groups import apply_form

    if _is_visible(page, apply_form.APPLY_RESUME_SELECT, timeout_ms=OPTIONAL_FIELD_TIMEOUT_MS):
        _select_resume_in_form(page, resume_id)

    if _is_visible(
        page, apply_form.APPLY_COVER_LETTER_TOGGLE, timeout_ms=OPTIONAL_FIELD_TIMEOUT_MS
    ):
        page.locator(apply_form.APPLY_COVER_LETTER_TOGGLE).click()
        # Клик раскрывает textarea — ждём её готовности явно, а не слепую паузу.

    if _is_visible(
        page, apply_form.APPLY_COVER_LETTER_TEXTAREA, timeout_ms=OPTIONAL_FIELD_TIMEOUT_MS
    ):
        page.locator(apply_form.APPLY_COVER_LETTER_TEXTAREA).fill(letter)
        # fill() синхронно выставляет значение — дополнительное ожидание не нужно.

    # Кнопка отправки — обязательный элемент формы. Не optional: отсутствие = отказ.
    if not _is_visible(page, apply_form.APPLY_SUBMIT_BUTTON, timeout_ms=APPLY_TIMEOUT_MS):
        return "кнопка отправки отклика не найдена в форме"

    page.locator(apply_form.APPLY_SUBMIT_BUTTON).click()
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

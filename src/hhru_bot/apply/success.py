"""Шаг: подтверждение успешной отправки отклика.

Владелец: #7. Селекторы success-сигналов живут здесь, изолированно от
APPLY_ALREADY_RESPONDED_MARKER (владение #3, в apply/dedup.py). Shared-селектор
submit-кнопки читается из selector_groups/apply_form (без правок того файла).

Multi-signal success: один отклик может подтверждаться любым из независимых
сигналов, т.к. реальная вёрстка hh.ru формы отклика НЕ подтверждена (рендерится
только залогиненному через JS). Подстраховываемся несколькими признаками:

  1. CSS success-маркер (основной + запасные) — first_locator по цепочке.
  2. Текст-признак «отклик отправлен» через page.get_by_text (для вариантов
     вёрстки, где подтверждение — текст, а не data-qa).
  3. Исчезновение submit-кнопки — после её клика в fill_response_form форма
     уходит; submit точно был (иначе fill_response_form уже отказал бы), so
     его исчезновение = успех.

Если ни один сигнал не сработал мгновенно — ждём основной маркер до таймаута
(медленный JS-рендер). На таймаут возвращаем False.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..selector_groups import apply_form
from .locators import first_locator

logger = logging.getLogger("hhru_bot.apply.success")

# Основной success-маркер — НЕ подтверждено (требует логина). Оставлен как
# отдельный символ для обратной совместимости (на него опирались тесты/импорты).
APPLY_SUCCESS_MARKER = "[data-qa='vacancy-response-sent-message']"

# Цепочка success-маркеров по убыванию приоритета. first_locator перебирает
# их по порядку — первый присутствующий подтверждает успех.
APPLY_SUCCESS_MARKERS = (
    APPLY_SUCCESS_MARKER,
    "[data-qa='vacancy-response-success']",
    ".bloko-modal-response-success",
)

# Текстовые признаки отправленного отклика. get_by_text ищет подстроку, поэтому
# достаточно устойчивой фразы; перечислены варианты, замеченные на hh.ru.
APPLY_SUCCESS_TEXTS = (
    "Отклик отправлен",
    "Вы откликнулись на вакансию",
)

# Submit-кнопка формы отклика — shared-селектор (читаем, не меняем apply_form).
APPLY_SUBMIT_SELECTOR = apply_form.APPLY_SUBMIT_BUTTON


def _signal_marker(page: Page) -> bool:
    """Сигнал 1: виден ли хоть один CSS success-маркер."""
    return first_locator(page, *APPLY_SUCCESS_MARKERS) is not None


def _signal_text(page: Page) -> bool:
    """Сигнал 2: есть ли на странице текст-признак отправленного отклика."""
    for phrase in APPLY_SUCCESS_TEXTS:
        if page.get_by_text(phrase).count() > 0:
            return True
    return False


def _signal_submit_gone(page: Page) -> bool:
    """Сигнал 3: submit-кнопка исчезла после отправки (форма ушла = успех).

    Имеет смысл только после клика по submit в fill_response_form — там уже
    проверено, что кнопка была. Здесь её отсутствие трактуем как успех.
    """
    return page.locator(APPLY_SUBMIT_SELECTOR).count() == 0


def wait_success_confirmation(page: Page, timeout_ms: int = 10_000) -> bool:
    """Подтверждает успех отклика по нескольким сигналам.

    Возвращает True, если сработал любой сигнал: success-маркер, текст
    «отклик отправлен» или исчезновение submit-кнопки. Если ни один не
    сработал мгновенно — ждёт основной маркер до timeout_ms и на таймаут
    возвращает False.
    """
    if _signal_marker(page):
        logger.debug("Success подтверждён: success-маркер")
        return True
    if _signal_text(page):
        logger.debug("Success подтверждён: текст-признак")
        return True
    if _signal_submit_gone(page):
        logger.debug("Success подтверждён: submit-кнопка исчезла")
        return True

    try:
        page.locator(APPLY_SUCCESS_MARKER).wait_for(timeout=timeout_ms)
    except PlaywrightTimeoutError:
        logger.warning("Не дождались ни одного сигнала успеха за %d мс", timeout_ms)
        return False
    return True

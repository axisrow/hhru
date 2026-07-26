"""Шаг: подтверждение успешной отправки отклика.

Владелец: #7. Селекторы success-сигналов живут здесь, изолированно от
APPLY_ALREADY_RESPONDED_MARKER (владение #3, в apply/dedup.py).

Multi-signal success: один отклик может подтверждаться любым из ПОЗИТИВНЫХ
сигналов, т.к. реальная вёрстка hh.ru формы отклика НЕ подтверждена (рендерится
только залогиненному через JS). Подстраховываемся несколькими признаками:

  1. CSS success-маркер (основной + запасные) — first_locator по цепочке.
  2. Текст-признак «отклик отправлен» через page.get_by_text (для вариантов
     вёрстки, где подтверждение — текст, а не data-qa).

Важно (отклонение от первоначального дизайна #7): успех подтверждается ТОЛЬКО
ПОЗИТИВНЫМИ сигналами — присутствием маркера/текста. «Исчезновение submit-кнопки»
(отрицательный признак) намеренно НЕ используется как самостоятельный сигнал:
после клика submit любая страница без submit-селектора (auth-redirect при
истёкшей сессии, CAPTCHA/challenge, ошибка валидации, throttle, maintenance,
пустой/битой DOM) дала бы false success, который запишется в историю (status=
'success'), а has_applied() навсегда исключит вакансию и сгорит дневной лимит.
Перечислить все неуспешные состояния для непроверенной вёрстки нельзя, поэтому
отрицательный признак здесь не источник True — только положительные маркеры.

Если ни один позитивный сигнал не сработал мгновенно — ждём основной маркер до
таймаута (медленный JS-рендер). На таймаут возвращаем False.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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

# Текстовые признаки отправленного отклика. Регистронезависимый regex (#7):
# вёрстка hh.ru не подтверждена, регистр/пунктуация могут различаться между
# ревизиями — точное сравнение промахнётся. get_by_text принимает Pattern.
APPLY_SUCCESS_TEXT_RE = re.compile(r"отклик отправлен|вы откликнулись", re.IGNORECASE)


def _signal_marker(page: Page) -> bool:
    """Сигнал 1: виден ли хоть один CSS success-маркер."""
    return first_locator(page, *APPLY_SUCCESS_MARKERS) is not None


def _signal_text(page: Page) -> bool:
    """Сигнал 2: есть ли на странице текст-признак отправленного отклика."""
    return page.get_by_text(APPLY_SUCCESS_TEXT_RE).count() > 0


def wait_success_confirmation(page: Page, timeout_ms: int = 10_000) -> bool:
    """Подтверждает успех отклика по позитивным сигналам.

    Возвращает True, если сработал любой позитивный сигнал: success-маркер
    (CSS-цепочка) или текст «отклик отправлен» (регистронезависимо). Отрица-
    тельный признак (исчезновение submit) успехом НЕ считается. Если ни один
    сигнал не сработал мгновенно — ждёт основной маркер до timeout_ms и на
    таймаут возвращает False.
    """
    if _signal_marker(page):
        logger.debug("Success подтверждён: success-маркер")
        return True
    if _signal_text(page):
        logger.debug("Success подтверждён: текст-признак")
        return True

    try:
        page.locator(APPLY_SUCCESS_MARKER).wait_for(timeout=timeout_ms)
    except PlaywrightTimeoutError:
        logger.warning(
            "Не дождались ни одного сигнала успеха за %d мс (url=%s)",
            timeout_ms,
            page.url,
        )
        return False
    return True

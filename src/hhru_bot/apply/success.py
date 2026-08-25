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

Если ни один позитивный сигнал не сработал мгновенно — опрашиваем UNION всех
сигналов (маркеры ИЛИ текст) в цикле до таймаута, т.к. hh.ru рендерит форму
асинхронно через JS и подтверждение может прийти через fallback-маркер/текст
позже первого опроса. На таймаут возвращаем False.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable

from playwright.sync_api import Page

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
    # Current Magritte response popup (captured in the post-submit form
    # dumps).  The old selectors above belong to the legacy popup and remain
    # as compatibility fallbacks.
    "[data-qa='responded-success-attach-cover-letter']",
    "[data-qa='vacancy-response-link-top']",
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


def _any_positive_signal(page: Page) -> str | None:
    """Union позитивных сигналов: возвращает описание сработавшего или None.

    Опрашивает ВСЕ позитивные сигналы (CSS-цепочка маркеров И текст), а не
    только основной маркер — т.к. hh.ru рендерит форму отклика асинхронно через
    JS, и подтверждение может прийти через fallback-маркер или текст, а не
    через основной data-qa.
    """
    if _signal_marker(page):
        return "success-маркер"
    if _signal_text(page):
        return "текст-признак"
    return None


def _sleep(page: Page, ms: float) -> None:
    """Пауза между опросами: page.wait_for_timeout (Playwright) либо time.sleep."""
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if callable(wait_for_timeout):
        wait_for_timeout(ms)
    else:  # pragma: no cover — fallback для не-Playwright page (напр. тесты без метода)
        time.sleep(ms / 1000)


# Шаг poll-loop между переодпросами сигналов (мс). Меньше — быстрее ловит
# async-рендер, больше — меньше накладных. 80 мс — баланс для ручного CLI.
_POLL_INTERVAL_MS = 80
# Success UI is rendered asynchronously after the submit/navigation completes.
# It is only a fast-path: when the marker does not appear, the pipeline checks
# /applicant/negotiations, which is the authoritative post-submit source. Keep
# this interval short and varied without increasing the request rate to hh.ru.
SUCCESS_CONFIRMATION_MIN_TIMEOUT_MS = 3_000
SUCCESS_CONFIRMATION_MAX_TIMEOUT_MS = 8_000


def wait_success_confirmation(
    page: Page,
    timeout_ms: int | None = None,
    terminal_check: Callable[[], None] | None = None,
) -> bool:
    """Подтверждает успех отклика по позитивным сигналам.

    Возвращает True, если в пределах timeout_ms появился любой позитивный
    сигнал: success-маркер (CSS-цепочка) ИЛИ текст «отклик отправлен»
    (регистронезависимо). Опрашивает UNION всех позитивных сигналов в цикле до
    таймаута — чтобы поймать асинхронно отрендеренный fallback-маркер или текст
    (основной data-qa может не появиться или задержаться на непроверенной
    JS-форме). Отрицательный признак (исчезновение submit) успехом НЕ считается.

    Если timeout_ms не задан, ожидание выбирается случайно в диапазоне 3–8
    секунд. Таймаут возвращает False (сигналов нет — локально успех не подтверждён),
    но с #207 это НЕ финальный вердикт: pipeline перед записью 'failed'
    перепроверяет отклик внешним источником (/applicant/negotiations, см.
    apply/verify.py) — найденный там отклик финализируется как success. Короткое
    окно не меняет fail-closed правило для сигналов и не повышает частоту submit.
    """
    if timeout_ms is None:
        timeout_ms = random.randint(
            SUCCESS_CONFIRMATION_MIN_TIMEOUT_MS,
            SUCCESS_CONFIRMATION_MAX_TIMEOUT_MS,
        )
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        # #344: a challenge may be the submit response itself. Check on every
        # poll so we stop immediately instead of waiting 30s and navigating on.
        if terminal_check is not None:
            terminal_check()
        if which := _any_positive_signal(page):
            logger.debug("Success подтверждён: %s", which)
            return True
        if time.monotonic() >= deadline:
            logger.warning(
                "Не дождались ни одного сигнала успеха за %d мс (url=%s)",
                timeout_ms,
                page.url,
            )
            return False
        _sleep(page, _POLL_INTERVAL_MS)

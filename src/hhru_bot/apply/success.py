"""Шаг: подтверждение успешной отправки отклика.

Владелец: #7. Селектор success-маркера живёт здесь, изолированно от
APPLY_ALREADY_RESPONDED_MARKER (владение #3, в apply/dedup.py).
"""

from __future__ import annotations

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# Маркер успешной отправки отклика — НЕ подтверждено (требует логина).
APPLY_SUCCESS_MARKER = "[data-qa='vacancy-response-sent-message']"


def wait_success_confirmation(page: Page, timeout_ms: int = 10_000) -> bool:
    """Ждёт появления маркера успешной отправки. True — подтверждено, False — таймаут."""
    try:
        page.locator(APPLY_SUCCESS_MARKER).wait_for(timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False
    return True

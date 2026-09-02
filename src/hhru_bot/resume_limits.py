"""Shared pre-write detection of the hh.ru resume quota."""

from __future__ import annotations

from playwright.sync_api import Locator

RESUME_QUOTA_UNREADABLE_REASON = "квоту прочитать не удалось — повторите попытку"
RESUME_LIMIT_REASON = RESUME_QUOTA_UNREADABLE_REASON


def resume_limit_reason(action: Locator) -> str:
    """Return an unreadable-quota failure unless the action is unambiguous.

    An absent or disabled action can be caused by a network/rendering failure.
    An exhausted result must come from an explicit hh.ru quota marker with
    parsed N/M values, never from this fallback.
    """
    count = action.count()
    if count != 1 or action.first.is_disabled():
        return RESUME_QUOTA_UNREADABLE_REASON
    return ""

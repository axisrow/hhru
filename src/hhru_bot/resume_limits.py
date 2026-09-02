"""Shared pre-write detection of the hh.ru resume quota."""

from __future__ import annotations

from playwright.sync_api import Locator

RESUME_LIMIT_REASON = "лимит резюме исчерпан; удалите ненужные резюме и повторите попытку"


def resume_limit_reason(action: Locator) -> str:
    """Return the quota failure when an action is absent or disabled.

    Callers must invoke this only after the relevant page has been hydrated.
    Before that point an absent action is merely an indeterminate DOM state;
    treating it as a quota would hide selector or loading failures.  More than
    one action is likewise left to the caller's ambiguity check.
    """
    count = action.count()
    if count == 0:
        return RESUME_LIMIT_REASON
    if count == 1 and action.first.is_disabled():
        return RESUME_LIMIT_REASON
    return ""

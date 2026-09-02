"""Shared resume-quota detection (#928)."""

import pytest

from hhru_bot.resume_limits import RESUME_QUOTA_UNREADABLE_REASON, resume_limit_reason

pytestmark = pytest.mark.unit


class _Locator:
    def __init__(self, count, disabled=False):
        self._count = count
        self._disabled = disabled
        self.first = self

    def count(self):
        return self._count

    def is_disabled(self):
        return self._disabled


@pytest.mark.parametrize("locator", [_Locator(0), _Locator(1, disabled=True), _Locator(2)])
def test_unreadable_action_never_reports_exhausted_quota(locator):
    reason = resume_limit_reason(locator)
    assert reason == RESUME_QUOTA_UNREADABLE_REASON
    assert "исчерпан" not in reason


def test_enabled_action_has_no_quota_reason():
    assert resume_limit_reason(_Locator(1)) == ""


def test_ambiguous_action_is_not_called_quota():
    assert resume_limit_reason(_Locator(2, disabled=True)) == RESUME_QUOTA_UNREADABLE_REASON

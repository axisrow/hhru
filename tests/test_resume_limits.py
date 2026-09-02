"""Shared resume-quota detection (#928)."""

import pytest

from hhru_bot.resume_limits import RESUME_LIMIT_REASON, resume_limit_reason

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


@pytest.mark.parametrize("locator", [_Locator(0), _Locator(1, disabled=True)])
def test_absent_or_disabled_action_reports_quota(locator):
    assert resume_limit_reason(locator) == RESUME_LIMIT_REASON


def test_enabled_action_has_no_quota_reason():
    assert resume_limit_reason(_Locator(1)) == ""


def test_ambiguous_action_is_not_called_quota():
    assert resume_limit_reason(_Locator(2, disabled=True)) == ""

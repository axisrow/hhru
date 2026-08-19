"""Regression test for _edit_block hydration-failure handling (#368 cycle-review
round 1, codex finding).

open_hydrated_resume_editor raises RuntimeError (not PlaywrightError) for
trigger-not-found/open-failed/wrong-route. Before the fix, _edit_block caught
only PlaywrightError, so a hydration failure on a later row escaped
edit_education_on_hh uncaught after an earlier row already saved — losing the
partial-save EducationResult (and the history record it would have produced).
"""

from __future__ import annotations

import pytest

from hhru_bot import resume_education
from hhru_bot.config_sections.education import EducationRecord
from hhru_bot.resume_education import _edit_block

pytestmark = pytest.mark.unit


class FakeSaveButton:
    def __init__(self, page):
        self._page = page

    def count(self):
        return 1

    def click(self):
        self._page.saved_rows.append(self._page.current_index)

    def wait_for_url(self, *args, **kwargs):  # noqa: ARG002
        pass


class FakeFieldLocator:
    def count(self):
        return 1

    def fill(self, value):  # noqa: ARG002
        pass


class FakeTrigger:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class FakePage:
    """Models only what _edit_block itself calls directly (trigger/button/field
    locators). open_hydrated_resume_editor is monkeypatched separately so this
    fake does not need to simulate its internal retry/route logic."""

    def __init__(self, *, trigger_count: int = 1, resume_id: str = "RID"):
        self._trigger_count = trigger_count
        self.saved_rows: list[int] = []
        self.current_index = -1
        self.url = f"https://hh.ru/resume/{resume_id}"

    def locator(self, selector: str):
        if selector.startswith("[data-qa='resume-edit-button-"):
            return FakeTrigger(self._trigger_count)
        if selector == "[data-qa='profile-layout-save-button']":
            return FakeSaveButton(self)
        return FakeFieldLocator()

    def wait_for_url(self, *args, **kwargs):  # noqa: ARG002
        pass


def test_hydration_runtime_error_after_prior_save_is_reported_not_raised(monkeypatch):
    """Codex finding on #368: row 0 hydrates and saves; row 1's
    open_hydrated_resume_editor raises RuntimeError (wrong-route/trigger-not-found
    class of failure). Before the fix this propagated out of _edit_block
    uncaught, losing the fact that row 0 already saved."""
    page = FakePage(trigger_count=1)
    calls = {"n": 0}

    def fake_open_hydrated_resume_editor(page_arg, **kwargs):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            page.current_index = 0
            return page.locator(next(iter(kwargs.get("editor_selector", "") or "x")))
        raise RuntimeError("форма образования 1 открыта не для того резюме")

    monkeypatch.setattr(
        resume_education, "open_hydrated_resume_editor", fake_open_hydrated_resume_editor
    )

    records = [
        EducationRecord(
            institution="A", level="", faculty="", organization="", specialty="", year="2020"
        ),
        EducationRecord(
            institution="B", level="", faculty="", organization="", specialty="", year="2021"
        ),
    ]

    result = _edit_block(page, records, additional=False, dry_run=False, resume_id="RID")

    # Row 0 saved before the RuntimeError on row 1.
    assert page.saved_rows == [0]
    # The function must return an EducationResult, not raise.
    assert result.success is False
    assert result.saved == 1
    assert result.uncertain is True
    assert "ошибка UI" in result.reason

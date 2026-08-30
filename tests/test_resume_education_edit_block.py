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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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

    def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
        pass


class FakeFieldLocator:
    def count(self):
        return 1

    def fill(self, value):  # noqa: ARG002
        pass

    def click(self, *, timeout=None):  # noqa: ARG002
        pass

    def or_(self, other):  # noqa: ARG002
        return self

    @property
    def first(self):
        return self

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        pass


class FakeAbsentLocator:
    """Models a locator with no matches -- e.g. the cookie banner, which is
    not present in these fakes' modeled DOM (#825: dismiss_cookie_banner is a
    best-effort no-op here, same as on a real page with no banner shown)."""

    def count(self):
        return 0


class FakeTrigger:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count

    def or_(self, other):  # noqa: ARG002
        return self

    @property
    def first(self):
        return self

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        pass


class FakePage:
    """Models only what _edit_block itself calls directly (trigger/button/field
    locators). open_hydrated_resume_editor is monkeypatched separately so this
    fake does not need to simulate its internal retry/route logic."""

    def __init__(
        self,
        *,
        trigger_count: int = 1,
        resume_id: str = "RID",
        wait_for_url_error: Exception | None = None,
        institution_found: bool = True,
    ):
        self._trigger_count = trigger_count
        self.saved_rows: list[int] = []
        self.current_index = -1
        self.url = f"https://hh.ru/resume/{resume_id}"
        # #825 review: wait_for_url_error lets a test model the exact case the
        # fix targets -- wait_for_url(wait_until="commit") timing out even
        # though the navigation (and save) already happened for real.
        self._wait_for_url_error = wait_for_url_error
        self._institution_found = institution_found

    def locator(self, selector: str):
        if selector.startswith("[data-qa='resume-edit-button-"):
            return FakeTrigger(self._trigger_count)
        if selector == "[data-qa='profile-layout-save-button']":
            return FakeSaveButton(self)
        if selector == "[data-qa='cookies-policy-informer-accept']":
            return FakeAbsentLocator()
        return FakeFieldLocator()

    def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
        if self._wait_for_url_error is not None:
            raise self._wait_for_url_error

    def get_by_text(self, text):  # noqa: ARG002
        # #825: the positive post-save check looks up the saved record's own
        # text on the page -- by default these fakes model a page where every
        # save trivially "shows up" (they don't render real DOM), matching
        # every existing test's expectation that a successful
        # FakeSaveButton.click() is a full, confirmed success.
        # institution_found=False lets a test model the opposite: the record's
        # text genuinely absent after save (the Magritte-race case #825 found).
        return FakeFieldLocator() if self._institution_found else FakeAbsentLocator()

    def content(self):
        return "<html></html>"

    def screenshot(self, *, path, full_page=None):  # noqa: ARG002
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


def test_edit_block_passes_expected_query_to_open_hydrated_resume_editor(monkeypatch):
    """#788: the education editor must bind the opened form to the requested
    resume_id via the `resumeFrom` query parameter, not only via the path."""
    page = FakePage(trigger_count=1, resume_id="resume-id")
    captured: dict = {}

    def fake_open_hydrated_resume_editor(page_arg, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return page.locator(next(iter(kwargs.get("editor_selector", "") or "x")))

    monkeypatch.setattr(
        resume_education, "open_hydrated_resume_editor", fake_open_hydrated_resume_editor
    )

    records = [
        EducationRecord(
            institution="A", level="", faculty="", organization="", specialty="", year="2020"
        ),
    ]

    _edit_block(page, records, additional=False, dry_run=False, resume_id="resume-id")

    assert captured.get("expected_query") == {"resumeFrom": "resume-id"}


def test_wait_for_url_timeout_with_confirmed_identity_and_text_is_success(monkeypatch):
    """#825: wait_for_url(wait_until='commit') can time out even though the
    SPA/pushState navigation already happened for real -- live investigation
    caught this exact case with a DOM dump (page.url already on the resume,
    the saved record already visible). Before this fix, any wait_for_url
    timeout was reported as uncertain regardless of the page's actual state.
    """
    page = FakePage(
        trigger_count=1,
        resume_id="RID",
        wait_for_url_error=PlaywrightTimeoutError("Timeout 20000ms exceeded"),
        institution_found=True,
    )

    def fake_open_hydrated_resume_editor(page_arg, **kwargs):  # noqa: ARG001
        page.current_index = 0
        return page.locator(next(iter(kwargs.get("editor_selector", "") or "x")))

    monkeypatch.setattr(
        resume_education, "open_hydrated_resume_editor", fake_open_hydrated_resume_editor
    )

    records = [
        EducationRecord(
            institution="МГУ", level="", faculty="", organization="", specialty="", year="2020"
        ),
    ]

    result = _edit_block(page, records, additional=False, dry_run=False, resume_id="RID")

    # The click reached hh.ru (save.click() ran) and page.url already matches
    # resume_id -- resume_identity_matches is true despite the timeout, and
    # the record's own text is confirmed visible, so this must be a real
    # success, not a false uncertain.
    assert page.saved_rows == [0]
    assert result.success is True
    assert result.uncertain is False
    assert result.saved == 1


def test_wait_for_url_timeout_with_confirmed_identity_but_missing_text_is_uncertain(monkeypatch):
    """#825: the flip side of the case above -- wait_for_url times out, the
    URL did move to the resume page, but the record's own text is genuinely
    absent (the Magritte combobox race #825 also found). This must stay
    uncertain, not be upgraded to success just because identity matched."""
    page = FakePage(
        trigger_count=1,
        resume_id="RID",
        wait_for_url_error=PlaywrightTimeoutError("Timeout 20000ms exceeded"),
        institution_found=False,
    )

    def fake_open_hydrated_resume_editor(page_arg, **kwargs):  # noqa: ARG001
        page.current_index = 0
        return page.locator(next(iter(kwargs.get("editor_selector", "") or "x")))

    monkeypatch.setattr(
        resume_education, "open_hydrated_resume_editor", fake_open_hydrated_resume_editor
    )

    records = [
        EducationRecord(
            institution="МГУ", level="", faculty="", organization="", specialty="", year="2020"
        ),
    ]

    result = _edit_block(page, records, additional=False, dry_run=False, resume_id="RID")

    assert result.success is False
    assert result.uncertain is True
    assert result.saved == 0
    assert "не отображается" in result.reason

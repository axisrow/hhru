"""Regression tests for #857: the additional-education block must fall back to
its resume-scoped direct route when the resume page renders no additional
card and no Add link.

Live probe (2026-08-30): hh.ru renders the additionalEducation card ONLY when
at least one additional entry is already attached to the resume (entries are
profile-level). A resume with zero attached entries has neither the card nor
the Add link, so the pre-#857 code failed with "блок образования не
отобразился" before ever reaching a save. The direct route
/resume/edit/{id}/additionalEducation still renders the full
resume-partial-edit form for such resumes (confirmed live), so the block
opens the form there instead of failing.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot import resume_education
from hhru_bot.config_sections.education import EducationRecord
from hhru_bot.resume_education import _edit_block

pytestmark = pytest.mark.unit


class _AlwaysReadyLocator:
    def count(self):
        return 1

    @property
    def first(self):
        return self

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        pass

    def fill(self, value):
        self._value = value

    def input_value(self):
        return getattr(self, "_value", "")

    def click(self):
        pass


class _NeverLocator:
    def __init__(self):
        self.count_reads = 0

    def count(self):
        self.count_reads += 1
        return 0

    @property
    def first(self):
        return self

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        raise PlaywrightTimeoutError("never renders")

    def or_(self, other):
        return _OrChain(self, other)


class _OrChain:
    def __init__(self, *parts):
        self._parts = parts

    def count(self):
        return sum(part.count() for part in self._parts)

    def or_(self, other):
        return _OrChain(*self._parts, other)

    @property
    def first(self):
        return self

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        for part in self._parts:
            if part.count() >= 1:
                return
        raise PlaywrightTimeoutError("no marker rendered")


class _FakePage:
    """No additional card and no Add link ever render (the #857 live case);
    the primary education card hydrates immediately; the direct route renders
    the save button."""

    def __init__(self, direct_url: str | None = None):
        self.url = "https://hh.ru/resume/RID"
        self._direct_url = direct_url

    def goto_direct(self, url: str):
        if self._direct_url is not None:
            self.url = self._direct_url

    def locator(self, selector: str):
        if selector == resume_education.PRIMARY_EDUCATION_CARD:
            return _AlwaysReadyLocator()
        if selector == resume_education.ADDITIONAL_SAVE_BUTTON:
            return _AlwaysReadyLocator()
        return _NeverLocator().or_(_NeverLocator())

    def get_by_text(self, text):  # noqa: ARG002
        class _T:
            def count(self):
                return 1

        return _T()

    def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
        pass

    def wait_for_timeout(self, timeout):  # noqa: ARG002
        pass

    def content(self):
        return "<html></html>"


_RECORD = EducationRecord(
    institution="Курс 857",
    level="",
    faculty="",
    organization="Орг 857",
    specialty="Спец",
    year="2020",
)


def _patch_common(monkeypatch, page: _FakePage):
    monkeypatch.setattr(resume_education, "goto_hh", lambda p, url: page.goto_direct(url))
    monkeypatch.setattr(resume_education, "labelled_field", lambda p, label: _AlwaysReadyLocator())
    monkeypatch.setattr(resume_education, "resume_identity_matches", lambda p, rid: True)
    monkeypatch.setattr(resume_education, "dismiss_cookie_banner", lambda p: None)


def test_additional_without_card_uses_direct_route(monkeypatch):
    page = _FakePage(direct_url="https://hh.ru/resume/edit/RID/additionalEducation")
    _patch_common(monkeypatch, page)

    result = _edit_block(page, [_RECORD], additional=True, dry_run=False, resume_id="RID")

    assert result.success is True, result.reason
    assert result.saved == 1


def test_primary_without_card_still_fails_closed(monkeypatch):
    page = _FakePage(direct_url="https://hh.ru/resume/edit/RID/additionalEducation")
    _patch_common(monkeypatch, page)

    result = _edit_block(page, [_RECORD], additional=False, dry_run=False, resume_id="RID")

    assert result.success is False
    assert result.uncertain is False
    assert "не отобразился" in result.reason


def test_direct_route_wrong_resume_fails_closed(monkeypatch):
    page = _FakePage(direct_url="https://hh.ru/resume/edit/OTHER/additionalEducation")
    _patch_common(monkeypatch, page)

    result = _edit_block(page, [_RECORD], additional=True, dry_run=False, resume_id="RID")

    assert result.success is False
    assert result.uncertain is False
    assert "не для того резюме" in result.reason


def test_direct_route_redirect_away_fails_closed(monkeypatch):
    page = _FakePage(direct_url="https://hh.ru/resume/RID")
    _patch_common(monkeypatch, page)

    result = _edit_block(page, [_RECORD], additional=True, dry_run=False, resume_id="RID")

    assert result.success is False
    assert result.uncertain is False


def test_direct_route_substring_resume_id_fails_closed(monkeypatch):
    """Guard must match resume_id as an exact path segment, not a substring.

    "234" is a Python substring of "1234", so a resume_id/URL pair like this
    would pass a naive ``resume_id in current_path`` check even though the
    form is open for a different resume (#862 review finding).
    """
    page = _FakePage(direct_url="https://hh.ru/resume/edit/1234/additionalEducation")
    _patch_common(monkeypatch, page)

    result = _edit_block(page, [_RECORD], additional=True, dry_run=False, resume_id="234")

    assert result.success is False
    assert result.uncertain is False
    assert "не для того резюме" in result.reason

"""Regression test for #812: _edit_block must wait for the education card to
hydrate before its first strict count() check.

``goto_hh`` only guarantees the URL committed, not that the resume page's
React SPA has rendered the education card yet (CLAUDE.md: "commit не значит
отрисовано"). Before this fix, _edit_block went straight to
``page.locator(trigger).count()`` / ``page.locator(add_selector).count()``
right after navigation, so a slow hydration made a correct card look
"selector not confirmed" -- a live-observed flaky failure (issue #812),
not a real selector drift.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot import resume_education
from hhru_bot.config_sections.education import EducationRecord
from hhru_bot.resume_education import _edit_block

pytestmark = pytest.mark.unit


class _OrWrapper:
    """Models Locator.or_(...).first.wait_for(...) for two underlying fakes."""

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback

    @property
    def first(self):
        return self

    def wait_for(self, *, state=None, timeout=None):  # noqa: ARG002
        # Mirrors real Playwright semantics closely enough for this test:
        # polls both sides a bounded number of times (simulating hydration
        # completing a couple of reads in) and times out only if neither ever
        # reports count() >= 1 -- the exact race the fix addresses.
        for _ in range(5):
            if self._primary.count() >= 1 or self._fallback.count() >= 1:
                return
        raise PlaywrightTimeoutError("Timeout waiting for locator")


class _HydratingLocator:
    """A locator whose count() only becomes truthy after N reads.

    Simulates the education card rendering asynchronously after goto_hh
    commits: the first few reads see an empty DOM, then hydration completes.
    """

    def __init__(self, ready_after: int, value_when_ready: int = 1):
        self._reads = 0
        self._ready_after = ready_after
        self._value_when_ready = value_when_ready

    def count(self):
        self._reads += 1
        return self._value_when_ready if self._reads > self._ready_after else 0

    def or_(self, other):
        return _OrWrapper(self, other)

    @property
    def first(self):
        return self

    def fill(self, value):  # noqa: ARG002
        pass

    def click(self):
        pass


class _NeverReadyLocator(_HydratingLocator):
    def __init__(self):
        super().__init__(ready_after=10_000)


class _RacyPage:
    """Fake page: PRIMARY_ADD hydrates after one throwaway read, trigger-0
    never appears (mirrors #812's confirmed live scenario -- empty education
    section, only the Add link is the real marker)."""

    def __init__(self):
        self.url = "https://hh.ru/resume/RID"
        self._trigger = _NeverReadyLocator()
        self._add = _HydratingLocator(ready_after=1)

    def locator(self, selector: str):
        if selector.startswith("[data-qa='resume-edit-button-"):
            return self._trigger
        if selector == resume_education.PRIMARY_ADD:
            return self._add
        return _HydratingLocator(ready_after=0)

    def wait_for_url(self, url, *, wait_until=None, timeout=None):  # noqa: ARG002
        pass


def test_race_resolves_once_add_link_hydrates(monkeypatch):
    """The pre-fix code read count() exactly once right after goto_hh and
    would see 0 on both trigger and add link -- reporting the false
    "confirmed Add link not found" failure from #812. The fix's explicit
    wait_for(state="visible") must let the race resolve instead."""
    page = _RacyPage()
    opened: dict = {}

    def fake_open_hydrated_resume_editor(page_arg, **kwargs):  # noqa: ARG001
        opened["trigger_selector"] = kwargs.get("trigger_selector")
        return page.locator(kwargs.get("editor_selector", "x"))

    monkeypatch.setattr(
        resume_education, "open_hydrated_resume_editor", fake_open_hydrated_resume_editor
    )

    records = [
        EducationRecord(
            institution="МГУ", level="", faculty="", organization="", specialty="", year="2015"
        ),
    ]

    result = _edit_block(page, records, additional=False, dry_run=True, resume_id="RID")

    assert result.success is True
    assert opened["trigger_selector"] == resume_education.PRIMARY_ADD


def test_wait_timeout_reports_failure_without_raising(monkeypatch):
    """If the card genuinely never hydrates (or a selector really drifted),
    the wait must time out into a normal EducationResult failure -- never an
    unhandled exception, keeping the fail-closed contract from #368."""

    class _StuckPage:
        def __init__(self):
            self.url = "https://hh.ru/resume/RID"

        def locator(self, selector: str):  # noqa: ARG002
            return _NeverReadyLocator()

    monkeypatch.setattr(
        resume_education,
        "open_hydrated_resume_editor",
        lambda *a, **k: pytest.fail("must not open the editor when the card never hydrates"),
    )

    records = [
        EducationRecord(
            institution="МГУ", level="", faculty="", organization="", specialty="", year="2015"
        ),
    ]

    result = _edit_block(_StuckPage(), records, additional=False, dry_run=True, resume_id="RID")

    assert result.success is False
    assert result.uncertain is False
    assert "не отобразился" in result.reason

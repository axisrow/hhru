"""Fail-closed post-click verification for delete-resume (#293)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hhru_bot.delete_resume as delete
from hhru_bot.selector_groups.resume_list import RESUME_LIST_CARD
from hhru_bot.selector_groups.resume_page import RESUME_DELETE_BUTTON, RESUME_DELETE_CONFIRM

pytestmark = pytest.mark.integration

RESUME_ID = "a" * 38
RESUME = SimpleNamespace(resume_id=RESUME_ID)


class Locator:
    def __init__(self, page, selector, count=1, detached_error=None):
        self.page = page
        self.selector = selector
        self._count = count
        self.detached_error = detached_error

    def count(self):
        return self._count

    def locator(self, selector):
        assert selector == RESUME_DELETE_BUTTON
        return Locator(self.page, selector)

    @property
    def first(self):
        return self

    def click(self):
        if self.selector == RESUME_DELETE_BUTTON:
            self.page.dialog_opened = True
        else:
            assert self.selector == RESUME_DELETE_CONFIRM
            self.page.clicked = True

    def wait_for(self, *, state, timeout):
        if state == "attached":
            self.page.ready_waited = timeout
        else:
            assert state == "detached"
            self.page.waited = timeout
        self.page.waited = timeout
        if self.detached_error:
            raise self.detached_error


class Page:
    def __init__(self, detached_error=None, readiness_error=None, remaining=0, ready_count=1):
        self.url = delete.RESUMES_LIST_URL
        self.dialog_opened = False
        self.clicked = False
        self.waited = None
        self.ready_waited = None
        self.reloaded = None
        self.detached_error = detached_error
        self.readiness_error = readiness_error
        self.remaining = remaining
        self.ready_count = ready_count

    def reload(self, *, wait_until):
        self.reloaded = wait_until

    def locator(self, selector):
        if selector == RESUME_DELETE_CONFIRM:
            return Locator(self, selector)
        if self.clicked:
            if selector == RESUME_LIST_CARD:
                return Locator(
                    self,
                    selector,
                    self.ready_count,
                    detached_error=self.readiness_error,
                )
            return Locator(self, selector, self.remaining)
        return Locator(self, selector, detached_error=self.detached_error)


def _patch_goto(monkeypatch):
    monkeypatch.setattr(delete, "goto_hh", lambda page, url: None)


def test_success_waits_for_identity_card_to_detach(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page()
    result = delete.delete_resume_on_hh(page, RESUME, dry_run=False)
    assert result.success is True
    assert result.uncertain is False
    assert page.clicked is True
    assert page.waited == 30_000
    assert page.reloaded == "domcontentloaded"
    assert page.ready_waited == 30_000


def test_post_click_verification_error_is_uncertain(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page(detached_error=RuntimeError("context closed"))
    result = delete.delete_resume_on_hh(page, RESUME, dry_run=False)
    assert result.success is False
    assert result.uncertain is True
    assert "проверить результат" in result.reason


def test_detachment_without_ready_list_is_uncertain(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page(ready_count=0, readiness_error=RuntimeError("list is still rendering"))
    result = delete.delete_resume_on_hh(page, RESUME, dry_run=False)
    assert result.success is False
    assert result.uncertain is True
    assert "проверить результат" in result.reason

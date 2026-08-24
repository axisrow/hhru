"""Fail-closed post-click verification for delete-resume (#293/#573)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage

import hhru_bot.delete_resume as delete
from hhru_bot.selector_groups.resume_list import RESUME_LIST_CARD
from hhru_bot.selector_groups.resume_page import RESUME_DELETE_BUTTON, RESUME_DELETE_CONFIRM

pytestmark = pytest.mark.integration

RESUME_ID = "a" * 38
RESUME = SimpleNamespace(resume_id=RESUME_ID)


class Locator:
    def __init__(self, page, selector, count=1, detached_error=None, hydration=False):
        self.page = page
        self.selector = selector
        self._count = count
        self.detached_error = detached_error
        self.hydration = hydration

    def count(self):
        if self.hydration and not self.page._recovery_hydrated:
            return 0
        return self._count

    def locator(self, selector):
        if selector == RESUME_DELETE_BUTTON:
            return Locator(self.page, selector, self.page.direct_count)
        raise AssertionError(selector)

    @property
    def first(self):
        return self

    def click(self, *, timeout=None):  # noqa: ARG002
        if self.selector == RESUME_DELETE_BUTTON:
            self.page.dialog_opened = True
        else:
            assert self.selector == RESUME_DELETE_CONFIRM
            self.page.clicked = True

    def wait_for(self, *, state, timeout):
        if state == "visible":
            if self.count() != 1:
                raise PlaywrightError("control not mounted")
            if (
                self.selector == RESUME_DELETE_CONFIRM
                and self.page.confirm_error
                and not self.page._confirm_failed
            ):
                self.page._confirm_failed = True
                raise self.page.confirm_error
            self.page.confirm_waited = timeout
            return
        if state == "attached":
            self.page.ready_waited = timeout
            if self.hydration:
                self.page._recovery_hydrated = True
        else:
            assert state == "detached"
            self.page.waited = timeout
        self.page.waited = timeout
        if self.detached_error:
            raise self.detached_error


class Page:
    def __init__(
        self,
        detached_error=None,
        readiness_error=None,
        remaining=0,
        ready_count=1,
        confirm_error=None,
        direct_count=1,
        profile_count=0,
    ):
        self.url = delete.RESUMES_FULL_LIST_URL
        self.dialog_opened = False
        self.clicked = False
        self.waited = None
        self.ready_waited = None
        self.confirm_waited = None
        self.reloaded = None
        self.detached_error = detached_error
        self.readiness_error = readiness_error
        self.remaining = remaining
        self.ready_count = ready_count
        self.confirm_error = confirm_error
        self._confirm_failed = False
        self.recovery_hydration = False
        self._recovery_hydrated = False
        self.direct_count = direct_count
        self.profile_count = profile_count
        self.profile_opened = False
        self.opened_resume_id = ""

    def reload(self, *, wait_until):
        self.reloaded = wait_until
        self.recovery_hydration = True

    def locator(self, selector):
        if selector == RESUME_DELETE_BUTTON:
            return Locator(self, selector, self.profile_count)
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
        if self.recovery_hydration:
            return Locator(self, selector, hydration=True)
        return Locator(self, selector, detached_error=self.detached_error)


def _patch_navigation(monkeypatch):
    monkeypatch.setattr(delete, "goto_hh", lambda page, url: None)

    def open_resume(page, resume_id):
        page.profile_opened = True
        page.opened_resume_id = resume_id

    monkeypatch.setattr(delete, "open_confirmed_resume", open_resume)


def test_success_waits_for_identity_card_to_detach(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page()
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is True
    assert result.uncertain is False
    assert page.clicked is True
    assert page.waited == 30_000
    assert page.reloaded == "domcontentloaded"
    assert page.ready_waited == 30_000


def test_post_click_verification_error_is_uncertain(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(detached_error=RuntimeError("context closed"))
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is False
    assert result.uncertain is True
    assert "проверить результат" in result.reason


def test_detachment_without_ready_list_is_uncertain(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(ready_count=0, readiness_error=RuntimeError("list is still rendering"))
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is False
    assert result.uncertain is True
    assert "проверить результат" in result.reason


def test_direct_recovery_reload_waits_for_card_hydration(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(confirm_error=PlaywrightError("confirm not mounted yet"))
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is True
    assert page.reloaded == "domcontentloaded"


def test_dry_run_resolves_direct_delete_action_without_profile(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page()
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is True
    assert page.profile_opened is False
    assert page.dialog_opened is False
    assert page.clicked is False


def test_published_resume_uses_identity_confirmed_profile_in_dry_run(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(direct_count=0, profile_count=1)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is True
    assert page.profile_opened is True
    assert page.opened_resume_id == RESUME_ID
    assert page.dialog_opened is False
    assert page.clicked is False


def test_profile_action_preserves_confirm_and_post_delete_verification(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(direct_count=0, profile_count=1)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is True
    assert result.uncertain is False
    assert page.profile_opened is True
    assert page.dialog_opened is True
    assert page.clicked is True


def test_profile_action_rejects_identity_mismatch_before_click(monkeypatch):
    _patch_navigation(monkeypatch)

    def reject_identity(page, resume_id):  # noqa: ARG001
        raise ValueError("identity резюме не подтверждён")

    monkeypatch.setattr(delete, "open_confirmed_resume", reject_identity)
    page = Page(direct_count=0, profile_count=1)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is False
    assert result.uncertain is False
    assert "identity резюме" in result.reason
    assert page.dialog_opened is False
    assert page.clicked is False


def test_profile_action_missing_fails_closed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(direct_count=0, profile_count=0)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is False
    assert result.uncertain is False
    assert "не появилась" in result.reason
    assert page.clicked is False


def test_profile_action_ambiguous_fails_closed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(direct_count=0, profile_count=2)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is False
    assert result.uncertain is False
    assert "неоднозначно" in result.reason
    assert page.clicked is False


def test_profile_action_recovers_before_confirm_click(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(
        direct_count=0,
        profile_count=1,
        confirm_error=PlaywrightError("confirm not mounted yet"),
    )
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is True
    assert result.uncertain is False
    assert page.profile_opened is True
    assert page.clicked is True


def test_ambiguous_direct_action_does_not_fall_back_to_profile(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(direct_count=2, profile_count=1)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is False
    assert result.uncertain is False
    assert "неоднозначно" in result.reason
    assert page.profile_opened is False
    assert page.clicked is False

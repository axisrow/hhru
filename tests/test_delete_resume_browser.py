"""Fail-closed post-click verification for delete-resume (#293)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage

import hhru_bot.delete_resume as delete
from hhru_bot.selector_groups.resume_list import (
    RESUME_DELETE_MENU_ACTION,
    RESUME_LIST_ACTION_MORE,
    RESUME_LIST_CARD,
)
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
        # Hydration locator: attached only after the recovery attached-wait.
        if self.hydration and not self.page._recovery_hydrated:
            return 0
        return self._count

    def locator(self, selector):
        if selector == RESUME_DELETE_BUTTON:
            return Locator(self.page, selector, self.page.direct_count)
        if selector == RESUME_LIST_ACTION_MORE:
            return Locator(self.page, selector, self.page.more_count)
        raise AssertionError(selector)

    @property
    def first(self):
        return self

    def click(self):
        if self.selector == RESUME_LIST_ACTION_MORE:
            self.page.menu_opened = True
        elif self.selector in (RESUME_DELETE_BUTTON, RESUME_DELETE_MENU_ACTION):
            self.page.dialog_opened = True
        else:
            assert self.selector == RESUME_DELETE_CONFIRM
            self.page.clicked = True

    def wait_for(self, *, state, timeout):
        if state == "visible":
            if self.selector == RESUME_DELETE_MENU_ACTION and not self.page.menu_opened:
                raise PlaywrightError("menu action not mounted")
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
        more_count=1,
        menu_count=0,
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
        self.more_count = more_count
        self.menu_count = menu_count
        self.menu_opened = False

    def reload(self, *, wait_until):
        self.reloaded = wait_until
        # After the recovery reload the card must be re-established through the
        # attached-wait before its count is trusted (commit-vs-hydration race).
        self.recovery_hydration = True

    def locator(self, selector):
        if selector == RESUME_DELETE_CONFIRM:
            return Locator(self, selector)
        if selector == RESUME_DELETE_MENU_ACTION:
            return Locator(self, selector, self.menu_count)
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


def _patch_goto(monkeypatch):
    monkeypatch.setattr(delete, "goto_hh", lambda page, url: None)


def test_success_waits_for_identity_card_to_detach(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page()
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is True
    assert result.uncertain is False
    assert page.clicked is True
    assert page.waited == 30_000
    assert page.reloaded == "domcontentloaded"
    assert page.ready_waited == 30_000


def test_post_click_verification_error_is_uncertain(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page(detached_error=RuntimeError("context closed"))
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is False
    assert result.uncertain is True
    assert "проверить результат" in result.reason


def test_detachment_without_ready_list_is_uncertain(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page(ready_count=0, readiness_error=RuntimeError("list is still rendering"))
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is False
    assert result.uncertain is True
    assert "проверить результат" in result.reason


def test_recovery_reload_waits_for_card_hydration(monkeypatch):
    # Первый клик подтверждения не дождался (hydration), recovery перезагрузил
    # страницу; после reload карточка появляется только через attached-wait, а
    # не как мгновенный count()==0 → ложный отказ «не подтверждена после
    # recovery reload». Без фикса (голый count()==0) удаление ошибочно падало бы.
    _patch_goto(monkeypatch)
    page = Page(confirm_error=PlaywrightError("confirm not mounted yet"))
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is True
    assert page.reloaded == "domcontentloaded"


def test_dry_run_resolves_direct_delete_action_without_menu(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page()
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is True
    assert page.menu_opened is False
    assert page.clicked is False


def test_dry_run_resolves_menu_delete_action(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page(direct_count=0, menu_count=1)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is True
    assert page.menu_opened is True
    assert page.clicked is False


def test_menu_delete_action_preserves_post_click_verification(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page(direct_count=0, menu_count=1)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=False)
    assert result.success is True
    assert result.uncertain is False
    assert page.menu_opened is True
    assert page.clicked is True


def test_missing_menu_delete_action_fails_closed(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page(direct_count=0, menu_count=0)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is False
    assert result.uncertain is False
    assert "действие удаления" in result.reason


def test_ambiguous_menu_delete_action_fails_closed(monkeypatch):
    _patch_goto(monkeypatch)
    page = Page(direct_count=0, menu_count=2)
    result = delete.delete_resume_on_hh(cast(PlaywrightPage, page), RESUME, dry_run=True)
    assert result.success is False
    assert result.uncertain is False
    assert "однозначно" in result.reason


def test_menu_text_fallback_is_scoped_to_portal_dialog():
    assert "[role='dialog'] button:has-text('Удалить резюме')" in RESUME_DELETE_MENU_ACTION
    assert ", button:has-text('Удалить резюме')" not in RESUME_DELETE_MENU_ACTION

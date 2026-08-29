"""Fail-closed exploration walk for report-vacancy (issue #745).

report_vacancy_on_hh() never submits the complaint (step 3 of the bloko-modal
wizard is intentionally unimplemented): success is always False. These tests
cover the fail-closed branches up to the confirmed step 2 (comment field).
"""

from __future__ import annotations

from typing import cast

import pytest
from playwright.sync_api import Page as PlaywrightPage
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import hhru_bot.report_vacancy as rv
from hhru_bot.selector_groups.vacancy_complain import (
    VACANCY_COMPLAIN_COMMENT_TEXTAREA,
    VACANCY_COMPLAIN_MENU_ITEM,
    VACANCY_COMPLAIN_MODAL,
    VACANCY_COMPLAIN_PAGE_BUTTON,
    VACANCY_COMPLAIN_WIZARD_NEXT,
    VACANCY_MORE_ACTIONS,
)

pytestmark = pytest.mark.unit

VACANCY_ID = "136672001"
REASON = "DOUBTFUL_VACANCY"
COMMENT = "test comment"


class Locator:
    def __init__(self, page, selector, count=1, disabled=False):
        self.page = page
        self.selector = selector
        self._count = count
        self._disabled = disabled

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def locator(self, selector):
        # Used for the bloko-radio label ancestor lookup — return self so
        # .click() below records the click against the radio selector.
        return self

    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self._count == 0:
            raise PlaywrightTimeoutError(f"not visible: {self.selector}")

    def is_disabled(self):
        return self._disabled

    def click(self):
        self.page.clicks.append(self.selector)
        self.page.on_click(self.selector)

    def fill(self, value):
        self.page.filled[self.selector] = value


class Page:
    def __init__(self, counts=None, disabled=None):
        self.url = "https://hh.ru/vacancy/136672001"
        self.counts = counts or {}
        self.disabled = disabled or {}
        self.clicks: list[str] = []
        self.filled: dict[str, str] = {}
        self.login_form = False

    def locator(self, selector):
        count = self.counts.get(selector, 1)
        is_disabled = self.disabled.get(selector, False)
        return Locator(self, selector, count=count, disabled=is_disabled)

    def on_click(self, selector):
        """Hook: menu item click reveals the page button; page button reveals modal."""
        if selector == VACANCY_COMPLAIN_MENU_ITEM:
            self.counts[VACANCY_COMPLAIN_PAGE_BUTTON] = self.counts.get(
                VACANCY_COMPLAIN_PAGE_BUTTON, 1
            )
        if selector == VACANCY_COMPLAIN_PAGE_BUTTON:
            self.counts[VACANCY_COMPLAIN_MODAL] = self.counts.get(VACANCY_COMPLAIN_MODAL, 1)
        if (
            selector == VACANCY_COMPLAIN_WIZARD_NEXT
            and VACANCY_COMPLAIN_COMMENT_TEXTAREA not in self.counts
        ):
            self.counts[VACANCY_COMPLAIN_COMMENT_TEXTAREA] = 1


def _patch_navigation(monkeypatch, login_form=False):
    monkeypatch.setattr(rv, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(rv, "has_login_form", lambda page: login_form)


def test_dry_run_never_clicks(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page()
    result = rv.report_vacancy_on_hh(
        cast(PlaywrightPage, page), VACANCY_ID, REASON, COMMENT, dry_run=True
    )
    assert result.success is False
    assert page.clicks == []
    assert "DRY-RUN" in result.reason_text


def test_unknown_reason_fails_closed_before_navigation(monkeypatch):
    calls = []
    monkeypatch.setattr(rv, "goto_hh", lambda page, url: calls.append(url))
    page = Page()
    result = rv.report_vacancy_on_hh(
        cast(PlaywrightPage, page), VACANCY_ID, "NOT_A_REASON", COMMENT, dry_run=False
    )
    assert result.success is False
    assert calls == []
    assert "не входит в подтверждённый перечень" in result.reason_text


def test_not_authenticated_raises(monkeypatch):
    _patch_navigation(monkeypatch, login_form=True)
    page = Page()
    with pytest.raises(rv.NotAuthenticated):
        rv.report_vacancy_on_hh(
            cast(PlaywrightPage, page), VACANCY_ID, REASON, COMMENT, dry_run=False
        )


def test_ambiguous_more_actions_fails_closed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(counts={VACANCY_MORE_ACTIONS: 2})
    result = rv.report_vacancy_on_hh(
        cast(PlaywrightPage, page), VACANCY_ID, REASON, COMMENT, dry_run=False
    )
    assert result.success is False
    assert result.uncertain is False
    assert "неоднозначна" in result.reason_text
    assert page.clicks == []


def test_disabled_menu_item_means_already_reported(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(disabled={VACANCY_COMPLAIN_MENU_ITEM: True})
    result = rv.report_vacancy_on_hh(
        cast(PlaywrightPage, page), VACANCY_ID, REASON, COMMENT, dry_run=False
    )
    assert result.success is False
    assert result.uncertain is False
    assert "уже была отправлена" in result.reason_text
    assert page.clicks == [VACANCY_MORE_ACTIONS]


def test_happy_path_reaches_step2_and_never_succeeds(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page()
    result = rv.report_vacancy_on_hh(
        cast(PlaywrightPage, page), VACANCY_ID, REASON, COMMENT, dry_run=False
    )
    # Design contract (issue #745): success is ALWAYS False — step 3 (submit)
    # is intentionally unimplemented and unconfirmed.
    assert result.success is False
    assert result.uncertain is False
    assert page.filled.get(VACANCY_COMPLAIN_COMMENT_TEXTAREA) == COMMENT
    assert "НЕ отправлена" in result.reason_text
    assert VACANCY_COMPLAIN_WIZARD_NEXT in page.clicks


def test_ambiguous_modal_fails_closed_without_uncertain(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(counts={VACANCY_COMPLAIN_MODAL: 2})
    result = rv.report_vacancy_on_hh(
        cast(PlaywrightPage, page), VACANCY_ID, REASON, COMMENT, dry_run=False
    )
    assert result.success is False
    assert result.uncertain is False
    assert "модалка жалобы неоднозначна" in result.reason_text


def test_playwright_error_before_step3_is_plain_failed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(counts={VACANCY_COMPLAIN_MODAL: 0})
    result = rv.report_vacancy_on_hh(
        cast(PlaywrightPage, page), VACANCY_ID, REASON, COMMENT, dry_run=False
    )
    assert result.success is False
    # No server-side mutation happens before the unimplemented step 3, so a
    # timeout here must not become a permanently blocking `uncertain`.
    assert result.uncertain is False

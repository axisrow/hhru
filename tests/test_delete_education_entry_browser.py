"""Fail-closed post-click verification for delete-education-entry (#802).

hh.ru's page.url does NOT change for a missing/deleted entry (live-confirmed
2026-08-30, see resume_education.py's _ENTRY_NOT_FOUND_TEXT comment) -- these
doubles model button presence/absence and the visible error-boundary text
instead of URL routing.
"""

from __future__ import annotations

from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage

import hhru_bot.resume_education as education
from hhru_bot.resume_education import _ENTRY_DELETE_TRIGGER, _ENTRY_NOT_FOUND_TEXT

pytestmark = pytest.mark.integration

ENTRY_ID = "8798959"


class Locator:
    def __init__(self, page, selector, count=1, detached_error=None):
        self.page = page
        self.selector = selector
        self._count = count
        self.detached_error = detached_error

    def count(self):
        if self.selector == _ENTRY_DELETE_TRIGGER["primary"]:
            return self.page.button_count
        return self._count

    @property
    def first(self):
        return self

    def click(self):
        self.page.clicked = True

    def wait_for(self, *, state, timeout):
        self.page.waited = (state, timeout)
        if state == "detached" and self.detached_error:
            raise self.detached_error


class Page:
    def __init__(
        self,
        *,
        button_count=1,
        button_count_after_click=0,
        not_found_text_count=0,
        detached_error=None,
    ):
        self.button_count = button_count
        self._button_count_after_click = button_count_after_click
        self.not_found_text_count = not_found_text_count
        self.detached_error = detached_error
        self.clicked = False
        self.waited = None
        self.goto_calls = []

    def locator(self, selector):
        if selector == _ENTRY_DELETE_TRIGGER["primary"]:
            return Locator(self, selector, self.button_count, self.detached_error)
        raise AssertionError(selector)

    def get_by_text(self, text):
        if text != _ENTRY_NOT_FOUND_TEXT:
            raise AssertionError(text)
        return Locator(self, text, self.not_found_text_count)


def _patch_navigation(monkeypatch, page):
    def goto(_target_page, url):
        page.goto_calls.append(url)

    monkeypatch.setattr(education, "goto_hh", goto)
    monkeypatch.setattr(education, "require_authenticated_page", lambda _target_page: None)


def test_dry_run_confirms_entry_without_clicking(monkeypatch):
    page = Page()
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=True
    )

    assert result.success is True
    assert result.uncertain is False
    assert result.not_found is False
    assert page.clicked is False
    assert page.goto_calls == [f"https://hh.ru/profile/edit/primaryEducation/{ENTRY_ID}"]


def test_entry_not_found_before_click_is_reported_as_success(monkeypatch):
    """A stale/already-deleted id must not block a retry (#802, avoids #480)."""
    page = Page(button_count=0, not_found_text_count=1)
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=True
    )

    assert result.success is True
    assert result.not_found is True
    assert page.clicked is False


def test_ambiguous_delete_button_fails_closed(monkeypatch):
    page = Page(button_count=2)
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=True
    )

    assert result.success is False
    assert result.uncertain is False
    assert result.not_found is False
    assert "однозначно" in result.reason
    assert page.clicked is False


def test_missing_delete_button_without_error_text_fails_closed(monkeypatch):
    """Button absent but no confirmed error text -- do not guess not_found."""
    page = Page(button_count=0, not_found_text_count=0)
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=True
    )

    assert result.success is False
    assert result.uncertain is False
    assert result.not_found is False
    assert page.clicked is False


def test_success_waits_for_delete_button_to_detach(monkeypatch):
    page = Page(button_count=1, button_count_after_click=0)
    _patch_navigation(monkeypatch, page)

    def click(_locator):
        page.clicked = True
        page.button_count = page._button_count_after_click

    monkeypatch.setattr(Locator, "click", click)
    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is True
    assert result.uncertain is False
    assert page.clicked is True
    assert page.waited == ("detached", education.ENTRY_DELETE_VERIFY_TIMEOUT_MS)


def test_click_exception_is_uncertain(monkeypatch):
    page = Page()
    _patch_navigation(monkeypatch, page)

    def click(_locator):
        raise PlaywrightError("navigation interrupted")

    monkeypatch.setattr(Locator, "click", click)
    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is False
    assert result.uncertain is True


def test_detach_timeout_is_uncertain(monkeypatch):
    page = Page(detached_error=PlaywrightError("still mounted"))
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is False
    assert result.uncertain is True
    assert "подтвердить результат" in result.reason


def test_button_still_present_after_detach_is_uncertain(monkeypatch):
    """Button detached (transition signal) but a fresh count() finds one
    again -- most likely an SPA re-mount of the same form, not a delete.
    """
    page = Page(button_count=1, button_count_after_click=1)
    _patch_navigation(monkeypatch, page)

    def click(_locator):
        page.clicked = True
        # button_count stays 1 -- detach fires (transition signal, mocked as
        # always succeeding here) but the re-check still finds the button.

    monkeypatch.setattr(Locator, "click", click)
    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is False
    assert result.uncertain is True
    assert "всё ещё присутствует" in result.reason


def test_before_click_hook_runs_immediately_before_the_destructive_click(monkeypatch):
    page = Page(button_count=1, button_count_after_click=0)
    _patch_navigation(monkeypatch, page)
    calls = []

    def click(_locator):
        calls.append("click")
        page.button_count = page._button_count_after_click

    monkeypatch.setattr(Locator, "click", click)
    education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page),
        "primary",
        ENTRY_ID,
        dry_run=False,
        before_click=lambda: calls.append("before_click"),
    )

    assert calls == ["before_click", "click"]


def test_invalid_kind_raises_value_error(monkeypatch):
    page = Page()
    _patch_navigation(monkeypatch, page)

    with pytest.raises(ValueError, match="primary"):
        education.delete_education_entry_on_hh(
            cast(PlaywrightPage, page), "attestation", ENTRY_ID, dry_run=True
        )

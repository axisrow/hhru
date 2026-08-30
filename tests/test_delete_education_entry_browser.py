"""Fail-closed post-click verification for delete-education-entry (#802, #809).

hh.ru's page.url does NOT change for a missing/deleted entry (live-confirmed
2026-08-30, see resume_education.py's _ENTRY_NOT_FOUND_TEXT comment) -- these
doubles model button presence/absence and the visible error-boundary text
instead of URL routing.

#809: the "Удалить" click opens a SECOND Magritte confirm dialog
(``[role='alertdialog']``) with no data-qa on it or its two buttons -- only
distinguishable by exact visible text ("Удалить"/"Отменить"), live-confirmed
2026-08-30 on a real authenticated session (cancelled via "Отменить", no
mutation performed). These doubles model that dialog via ``dialog_shown`` and
``dialog_button_count``.
"""

from __future__ import annotations

from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page as PlaywrightPage

import hhru_bot.resume_education as education
from hhru_bot.resume_education import (
    _DELETE_CONFIRM_DIALOG,
    _ENTRY_DELETE_TRIGGER,
    _ENTRY_NOT_FOUND_TEXT,
)

pytestmark = pytest.mark.integration

ENTRY_ID = "8798959"


class Locator:
    def __init__(self, page, selector, count=1, detached_error=None, text=None):
        self.page = page
        self.selector = selector
        self._count = count
        self.detached_error = detached_error
        self._text = text

    def _is_hydrated(self):
        if self.page.hydrate_after_clicks == 0:
            return True
        return self.page.click_count >= self.page.hydrate_after_clicks

    def count(self):
        if self.selector == _ENTRY_DELETE_TRIGGER["primary"]:
            return self.page.button_count
        if self.selector == _DELETE_CONFIRM_DIALOG:
            return 1 if (self.page.dialog_shown and self._is_hydrated()) else 0
        return self._count

    @property
    def first(self):
        return self

    def click(self):
        self.page.clicked = True
        if self.selector == _ENTRY_DELETE_TRIGGER["primary"]:
            self.page.click_count += 1

    def wait_for(self, *, state, timeout):
        self.page.waited.append((self.selector, state, timeout))
        if state == "detached" and self.selector == _ENTRY_DELETE_TRIGGER["primary"]:
            # Models the transition signal only -- a re-mount after detach
            # (button_count still != 0) is checked separately via count(),
            # same as production code's post-wait re-check. Before hydration
            # (#809), the trigger neither detaches nor errors -- it simply
            # stays mounted, so an unhydrated retry attempt must time out here
            # exactly like a real un-hydrated click would.
            if self.detached_error:
                raise self.detached_error
            if not self._is_hydrated():
                raise PlaywrightError("not hydrated yet")
            return
        if state == "visible" and self.selector == _DELETE_CONFIRM_DIALOG:
            if not (self.page.dialog_shown and self._is_hydrated()):
                raise PlaywrightError("dialog did not appear")
            return

    def get_by_text(self, text, *, exact=False):
        assert exact is True
        assert self.selector == _DELETE_CONFIRM_DIALOG
        return DialogButton(self.page, text)


class DialogButton(Locator):
    def __init__(self, page, text):
        super().__init__(page, f"dialog-text:{text}")
        self._text = text

    def count(self):
        if self._text != "Удалить":
            return 0
        return self.page.dialog_button_count

    def click(self):
        self.page.dialog_confirm_clicked = True
        # Confirming the dialog is what actually removes the entry in these
        # doubles -- matches live hh.ru tearing the whole form down once the
        # entry is gone.
        self.page.button_count = self.page.button_count_after_confirm
        self.page.dialog_shown = False


class Page:
    def __init__(
        self,
        *,
        button_count=1,
        button_count_after_click=0,
        not_found_text_count=0,
        detached_error=None,
        dialog_shown=False,
        dialog_button_count=1,
        button_count_after_confirm=0,
        hydrate_after_clicks=0,
    ):
        self.button_count = button_count
        self._button_count_after_click = button_count_after_click
        self.not_found_text_count = not_found_text_count
        self.detached_error = detached_error
        self.dialog_shown = dialog_shown
        self.dialog_button_count = dialog_button_count
        self.button_count_after_confirm = button_count_after_confirm
        self.clicked = False
        self.click_count = 0
        self.dialog_confirm_clicked = False
        self.waited = []
        self.goto_calls = []
        # #809: models the "not hydrated yet" click-retry race -- the first
        # N clicks on the trigger button are silent no-ops (neither the
        # dialog appears nor the row is torn down); the dialog only shows
        # starting from click number ``hydrate_after_clicks``.
        self.hydrate_after_clicks = hydrate_after_clicks

    def locator(self, selector):
        if selector == _ENTRY_DELETE_TRIGGER["primary"]:
            return Locator(self, selector, self.button_count, self.detached_error)
        if selector == _DELETE_CONFIRM_DIALOG:
            return Locator(self, selector)
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


def test_success_with_confirm_dialog(monkeypatch):
    """#809: the common case -- clicking 'Удалить' opens the second dialog,
    and confirming it inside the dialog is what actually removes the entry.
    """
    page = Page(dialog_shown=True, dialog_button_count=1, button_count_after_confirm=0)
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is True
    assert result.uncertain is False
    assert page.clicked is True
    assert page.dialog_confirm_clicked is True


def test_success_without_confirm_dialog_falls_back_to_detach(monkeypatch):
    """#809: unconfirmed for 'additional' -- no dialog, first click deletes
    directly. The button detaching alone must still resolve to success.
    """
    page = Page(button_count=1, button_count_after_click=0, dialog_shown=False)
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


def test_hydration_lag_retries_click_until_dialog_appears(monkeypatch):
    """#809: the id-scoped edit route can render the button visible well
    before React attaches its click handler (live-confirmed race). The first
    click(s) must be silent no-ops that the retry loop recovers from, not an
    immediate uncertain failure.
    """
    page = Page(
        dialog_shown=True,
        dialog_button_count=1,
        button_count_after_confirm=0,
        hydrate_after_clicks=3,
    )
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is True
    assert result.uncertain is False
    assert page.click_count == 3
    assert page.dialog_confirm_clicked is True


def test_before_click_hook_runs_exactly_once_across_hydration_retries(monkeypatch):
    """#809 (AO review on PR #816): the reservation happens once, before the
    loop's first click -- not per retry attempt. Multiple non-landing clicks
    while hydration catches up must not reserve (or double-reserve) anything.
    """
    page = Page(
        dialog_shown=True,
        dialog_button_count=1,
        button_count_after_confirm=0,
        hydrate_after_clicks=3,
    )
    _patch_navigation(monkeypatch, page)
    calls = []

    education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page),
        "primary",
        ENTRY_ID,
        dry_run=False,
        before_click=lambda: calls.append("before_click"),
    )

    assert calls == ["before_click"]
    assert page.click_count == 3


def test_hydration_lag_exhausting_retries_is_uncertain(monkeypatch):
    """#809: if hydration never completes within the retry budget, the
    outcome must still fail closed as uncertain rather than loop forever or
    silently report a false success.
    """
    page = Page(
        dialog_shown=True,
        dialog_button_count=1,
        button_count_after_confirm=0,
        hydrate_after_clicks=education.DELETE_CLICK_MAX_ATTEMPTS + 1,
    )
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is False
    assert result.uncertain is True
    assert page.click_count == education.DELETE_CLICK_MAX_ATTEMPTS
    assert page.dialog_confirm_clicked is False


def test_neither_dialog_nor_detach_after_click_is_uncertain(monkeypatch):
    """#809: click reached hh.ru, but neither expected outcome rendered."""
    page = Page(button_count=1, dialog_shown=False, detached_error=PlaywrightError("timeout"))
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is False
    assert result.uncertain is True
    assert "не подтверждён ни диалог" in result.reason


def test_dialog_confirm_button_not_found_fails_closed(monkeypatch):
    """#809: dialog appeared, but its confirm button is not exactly one match."""
    page = Page(dialog_shown=True, dialog_button_count=0)
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is False
    assert result.uncertain is True
    assert "кнопка подтверждения" in result.reason
    assert page.dialog_confirm_clicked is False


def test_dialog_confirm_click_exception_is_uncertain(monkeypatch):
    page = Page(dialog_shown=True, dialog_button_count=1)
    _patch_navigation(monkeypatch, page)

    def click(_locator):
        raise PlaywrightError("navigation interrupted")

    monkeypatch.setattr(DialogButton, "click", click)
    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is False
    assert result.uncertain is True


def test_button_still_present_after_dialog_confirm_is_uncertain(monkeypatch):
    """Dialog confirmed, but the trigger is still present afterwards --
    most likely an SPA re-mount, not an actual deletion.
    """
    page = Page(
        dialog_shown=True,
        dialog_button_count=1,
        button_count=1,
        button_count_after_confirm=1,
    )
    _patch_navigation(monkeypatch, page)

    result = education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page), "primary", ENTRY_ID, dry_run=False
    )

    assert result.success is False
    assert result.uncertain is True
    assert "всё ещё присутствует" in result.reason


def test_before_click_hook_runs_before_the_first_click_with_dialog(monkeypatch):
    """#809 (AO review on PR #816): before_click must reserve the durable
    intent BEFORE the first click, not after any click -- a crash between a
    possibly-destructive click and a retroactive before_click() call would
    otherwise leave no recorded row at all despite a real hh.ru mutation.
    kind="primary" confirms the first click only opens the dialog, but the
    reservation must not depend on that per-kind knowledge.
    """
    page = Page(dialog_shown=True, dialog_button_count=1, button_count_after_confirm=0)
    _patch_navigation(monkeypatch, page)
    calls = []

    def outer_click(_locator):
        calls.append("outer_click")

    def confirm_click(locator):
        calls.append("confirm_click")
        locator.page.button_count = locator.page.button_count_after_confirm
        locator.page.dialog_shown = False

    monkeypatch.setattr(Locator, "click", outer_click)
    monkeypatch.setattr(DialogButton, "click", confirm_click)
    education.delete_education_entry_on_hh(
        cast(PlaywrightPage, page),
        "primary",
        ENTRY_ID,
        dry_run=False,
        before_click=lambda: calls.append("before_click"),
    )

    assert calls == ["before_click", "outer_click", "confirm_click"]


def test_before_click_hook_runs_before_the_first_click_without_dialog(monkeypatch):
    """#809 (AO review on PR #816): same early reservation for the no-dialog
    path (kind="additional", unconfirmed whether the first click itself is
    destructive) -- reserving before the click is strictly safer than after.
    """
    page = Page(button_count=1, button_count_after_click=0, dialog_shown=False)
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

"""Unit-тесты browser-step смены видимости и стоп-листа работодателей (#746).

Селекторы подтверждены живым DOM 2026-08-29 (issue #746); эти тесты
проверяют чистую логику ``set_resume_visibility_on_hh`` через MagicMock-двойник
Playwright Page/Locator — по тому же паттерну, что ``test_resume_position.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import hhru_bot.resume_visibility as rv
from hhru_bot.config import bare_resume
from hhru_bot.selector_groups import resume_visibility as sel

pytestmark = pytest.mark.unit

RESUME_ID = "a" * 38


def _resume():
    return bare_resume(RESUME_ID)


def _mock_locator(count: int = 1):
    loc = MagicMock()
    loc.count.return_value = count
    loc.first = loc
    return loc


def test_dry_run_lists_planned_changes_without_touching_page():
    page = MagicMock()
    result = rv.set_resume_visibility_on_hh(
        page, _resume(), "whitelist", dry_run=True, add_employers=("Ксамата",)
    )
    assert result.success
    assert "режим будет изменён" in result.reason
    assert "будет добавлен работодатель «Ксамата»" in result.reason
    page.goto.assert_not_called()
    page.locator.assert_not_called()


def test_dry_run_with_no_changes_fails_closed():
    page = MagicMock()
    result = rv.set_resume_visibility_on_hh(page, _resume(), None, dry_run=True)
    assert not result.success
    assert "не задано" in result.reason


def test_unknown_mode_rejected_before_any_navigation():
    page = MagicMock()
    result = rv.set_resume_visibility_on_hh(page, _resume(), "public", dry_run=False)
    assert not result.success
    assert "неизвестный режим" in result.reason
    page.goto.assert_not_called()


def test_mode_only_change_clicks_label_and_save(monkeypatch):
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    mode_label = _mock_locator()
    locators = {
        sel.RESUME_VISIBILITY_SAVE: save,
        sel.RESUME_VISIBILITY_MODE_LINK_ONLY: mode_label,
    }
    page = MagicMock()
    page.locator.side_effect = lambda selector: locators[selector]
    before_click = MagicMock()

    # wait_for on SAVE toggles from "visible ready" to "hidden after click":
    # the same mock object is waited on twice with different states.
    result = rv.set_resume_visibility_on_hh(
        page, _resume(), "link-only", dry_run=False, before_click=before_click
    )

    assert result.success
    mode_label.click.assert_called_once_with()
    before_click.assert_called_once_with()
    save.click.assert_called_once_with()


def test_employer_list_edit_requires_active_list_mode(monkeypatch):
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()

    def _unchecked_mode_label():
        label = _mock_locator()
        radio_input = _mock_locator()
        radio_input.is_checked.return_value = False
        label.locator.return_value = radio_input
        return label

    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        sel.RESUME_VISIBILITY_MODE_WHITELIST: _unchecked_mode_label(),
        sel.RESUME_VISIBILITY_MODE_BLACKLIST: _unchecked_mode_label(),
    }[selector]

    result = rv.set_resume_visibility_on_hh(
        page, _resume(), None, dry_run=False, add_employers=("Ксамата",)
    )
    assert not result.success
    assert "не whitelist/blacklist" in result.reason


def test_add_employer_ambiguous_match_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    activator = _mock_locator()
    search_input = _mock_locator()

    result_items = MagicMock()
    result_items.count.return_value = 2

    def _make_item(employer_id: str, name: str):
        item = MagicMock()
        item.get_attribute.side_effect = lambda attr: (
            f"{sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX}{employer_id}"
            if attr == "data-qa"
            else None
        )
        name_locator = MagicMock()
        name_locator.count.return_value = 1
        name_locator.first.text_content.return_value = name
        item.locator.return_value = name_locator
        return item

    item_a = _make_item("3529", "СБЕР")
    item_b = _make_item("9001", "СБЕР")
    result_items.all.return_value = [item_a, item_b]

    def _by_selector(selector):
        if selector == sel.RESUME_VISIBILITY_SAVE:
            return save
        if selector == sel.RESUME_VISIBILITY_MODE_WHITELIST:
            return _mock_locator()
        if selector == sel.RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_WHITELIST:
            return activator
        if selector == sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT:
            return search_input
        if selector == sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX:
            return result_items
        raise AssertionError(f"unexpected selector {selector}")

    page = MagicMock()
    page.locator.side_effect = _by_selector

    result = rv.set_resume_visibility_on_hh(
        page, _resume(), "whitelist", dry_run=False, add_employers=("СБЕР",)
    )

    assert not result.success
    assert result.ambiguous_query == "СБЕР"
    assert {c.employer_id for c in result.ambiguous_candidates} == {"3529", "9001"}
    # No checkbox interaction happened for an ambiguous match — a false pick
    # here would silently stop-list the wrong company (issue #746).
    item_a.locator.return_value.check.assert_not_called()
    item_b.locator.return_value.check.assert_not_called()


def test_add_employer_single_match_checks_and_confirms(monkeypatch):
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    activator = _mock_locator()
    search_input = _mock_locator()
    close = _mock_locator()
    confirm = _mock_locator()

    result_items = MagicMock()
    result_items.count.return_value = 1
    item = MagicMock()
    item.get_attribute.side_effect = lambda attr: (
        f"{sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX}655542"
        if attr == "data-qa"
        else None
    )
    name_locator = MagicMock()
    name_locator.count.return_value = 1
    name_locator.first.text_content.return_value = "ЮMoney"
    item.locator.return_value = name_locator
    result_items.all.return_value = [item]

    row = MagicMock()
    row.count.return_value = 1
    row.first = row
    checkbox = MagicMock()
    checkbox.count.return_value = 1
    checkbox.first = checkbox
    row.locator.return_value = checkbox

    row_selector = (
        f"[data-qa='{sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX}655542']"
    )

    def _by_selector(selector):
        return {
            sel.RESUME_VISIBILITY_SAVE: save,
            sel.RESUME_VISIBILITY_MODE_WHITELIST: _mock_locator(),
            sel.RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_WHITELIST: activator,
            sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT: search_input,
            sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX: result_items,
            row_selector: row,
            sel.RESUME_VISIBILITY_MODAL_CONFIRM: confirm,
            sel.RESUME_VISIBILITY_MODAL_CLOSE: close,
        }[selector]

    page = MagicMock()
    page.locator.side_effect = _by_selector

    result = rv.set_resume_visibility_on_hh(
        page, _resume(), "whitelist", dry_run=False, add_employers=("ЮMoney",)
    )

    assert result.success
    search_input.fill.assert_called_once_with("ЮMoney")
    checkbox.check.assert_called_once_with()
    confirm.click.assert_called_once_with()
    close.click.assert_called_once_with()
    save.click.assert_called_once_with()


def test_remove_employer_not_found_fails_closed(monkeypatch):
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    activator = _mock_locator()
    search_input = _mock_locator()

    list_items = MagicMock()
    list_items.all.return_value = []

    def _by_selector(selector):
        return {
            sel.RESUME_VISIBILITY_SAVE: save,
            sel.RESUME_VISIBILITY_MODE_BLACKLIST: _mock_locator(),
            sel.RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST: activator,
            sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT: search_input,
            sel.RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX: list_items,
        }[selector]

    page = MagicMock()
    page.locator.side_effect = _by_selector

    result = rv.set_resume_visibility_on_hh(
        page, _resume(), "blacklist", dry_run=False, remove_employers=("Неизвестная Компания",)
    )
    assert not result.success
    assert "не найден в текущем списке" in result.reason


def test_click_failure_after_save_is_uncertain_not_failed(monkeypatch):
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    from playwright.sync_api import Error as PlaywrightError

    save = MagicMock()
    save.count.return_value = 1
    save.first = save
    # First wait_for (readiness) succeeds; the second one (post-click "hidden")
    # raises, mirroring #163/#176's fail-closed acted=True+uncertain contract.
    save.wait_for.side_effect = [None, PlaywrightError("timeout")]
    mode_label = _mock_locator()

    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        sel.RESUME_VISIBILITY_MODE_NO_ONE: mode_label,
    }[selector]

    result = rv.set_resume_visibility_on_hh(page, _resume(), "no-one", dry_run=False)
    assert not result.success
    assert result.uncertain

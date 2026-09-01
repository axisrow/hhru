"""Unit-тесты browser-step смены видимости и стоп-листа работодателей (#746).

Селекторы подтверждены живым DOM 2026-08-29 (issue #746); эти тесты
проверяют чистую логику ``set_resume_visibility_on_hh`` через MagicMock-двойник
Playwright Page/Locator — по тому же паттерну, что ``test_resume_position.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

import hhru_bot.resume_visibility as rv
from hhru_bot.config import bare_resume
from hhru_bot.selector_groups import resume_visibility as sel

pytestmark = pytest.mark.unit

RESUME_ID = "a" * 38


def _resume():
    return bare_resume(RESUME_ID)


def _mock_locator(count: int = 1, *, radio_checked: bool = True):
    """A generic locator mock; also stands in for a mode label whose nested
    radio input is checked by default (the post-#746-round-3 verification in
    _click_mode reads `.locator(RESUME_VISIBILITY_MODE_RADIO)` after every
    click). bounding_box — карточка режима (живой замер #901: 690x56)."""
    loc = MagicMock()
    loc.count.return_value = count
    loc.first = loc
    loc.bounding_box.return_value = {"x": 0, "y": 0, "width": 690, "height": 56}
    radio = MagicMock()
    radio.count.return_value = 1
    radio.first = radio
    radio.is_checked.return_value = radio_checked
    loc.locator.return_value = radio
    return loc


def _all_mode_labels(active: str | None = None) -> dict[str, MagicMock]:
    """Все пять карточек режимов; у «active» внешний radio checked, у остальных нет.

    read_active_mode (#901) перебирает все карточки — и при детекции активного
    whitelist/blacklist, и в перечитке после Save, — поэтому словарь
    page.locator обязан отвечать на каждый селектор режима."""
    return {
        rv._MODE_SELECTORS[mode]: _mock_locator(radio_checked=(mode == active))
        for mode in rv.VISIBILITY_MODES
    }


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
    mode_labels = _all_mode_labels(active="link-only")
    mode_label = mode_labels[sel.RESUME_VISIBILITY_MODE_LINK_ONLY]
    locators = {
        sel.RESUME_VISIBILITY_SAVE: save,
        **mode_labels,
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
    # Клик по карточке режима — в левую padding-зону (живой замер #901/#917:
    # центр карточки перехватывается вложенным label[data-qa='cell'] без
    # обновления React-состояния), а не в центр bounding box.
    mode_label.click.assert_called_once_with(position={"x": 10, "y": 28.0})
    before_click.assert_called_once_with()
    save.click.assert_called_once_with()


def test_mode_click_not_reflected_in_radio_fails_closed(monkeypatch):
    """Regression for #746 review round 3: a click that silently misses its
    target (stale locator, intercepting overlay) must not be trusted — the
    radio's .checked state is the source of truth, not "the click happened"."""
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    mode_label = _mock_locator(radio_checked=False)
    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        sel.RESUME_VISIBILITY_MODE_LINK_ONLY: mode_label,
    }[selector]

    result = rv.set_resume_visibility_on_hh(page, _resume(), "link-only", dry_run=False)

    assert not result.success
    assert "не подтверждён" in result.reason
    mode_label.click.assert_called_once_with(position={"x": 10, "y": 28.0})
    save.click.assert_not_called()


def test_employer_list_edit_requires_active_list_mode(monkeypatch):
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    # Ни один режим не checked — активный не whitelist/blacklist (#901:
    # read_active_mode читает внешние radio всех пяти карточек).
    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        **_all_mode_labels(active=None),
    }[selector]

    result = rv.set_resume_visibility_on_hh(
        page, _resume(), None, dry_run=False, add_employers=("Ксамата",)
    )
    assert not result.success
    assert "не whitelist/blacklist" in result.reason


def test_employer_list_detection_playwright_error_is_plain_fail(monkeypatch):
    """Ревью PR #917: пре-кликовая детекция активного режима — НЕ серая зона
    (мутации ещё не было, before_click не звали): PlaywrightError обязан
    стать обычным failed-результатом с per-resume [FAIL], а не сырым
    исключением, обрывающим --resume all batch."""
    from playwright.sync_api import Error as PlaywrightError

    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    broken_card = MagicMock()
    broken_radio = MagicMock()
    broken_radio.count.side_effect = PlaywrightError("Target closed")
    broken_card.locator.return_value = broken_radio

    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        **{s: broken_card for s in rv._MODE_SELECTORS.values()},
    }[selector]

    result = rv.set_resume_visibility_on_hh(
        page, _resume(), None, dry_run=False, add_employers=("Ксамата",)
    )
    assert not result.success
    assert not result.uncertain
    assert "активный режим не прочитан" in result.reason
    save.click.assert_not_called()


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
            **_all_mode_labels(active="whitelist"),
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
    # #746 review (AO reviewer): the query is cleared after a successful add so
    # a later --remove-employer call in the same run sees the already-added
    # list, not the still-filtered search results.
    search_input.fill.assert_has_calls([call("ЮMoney"), call("")])
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


def _list_item(employer_id: str, name: str):
    """A row of an already-added employer, per the #746 probe: `cell-text-content`
    holds the clean name, while item.text_content() would concatenate it twice
    (span + wrapping <a> render the same name) — the fix reads only the span."""
    item = MagicMock()
    item.get_attribute.side_effect = lambda attr: (
        f"{sel.RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DATA_QA_PREFIX}{employer_id}"
        if attr == "data-qa"
        else None
    )
    item.text_content.return_value = name + name  # span + <a> duplicate the name
    name_locator = MagicMock()
    name_locator.count.return_value = 1
    name_locator.first.text_content.return_value = name
    item.locator.return_value = name_locator
    delete_button = MagicMock()
    delete_button.count.return_value = 1
    delete_button.first = delete_button
    item.locator.side_effect = lambda sel_: (
        name_locator if sel_ == "[data-qa='cell-text-content']" else delete_button
    )
    return item, delete_button


def test_remove_employer_requires_exact_name_not_substring(monkeypatch):
    """Regression for #746 review round 2: a substring match on the raw row text
    could silently remove the wrong company (e.g. "Сбер" matching "Сбербанк")."""
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    activator = _mock_locator()
    search_input = _mock_locator()

    sberbank_item, sberbank_delete = _list_item("1", "Сбербанк")

    list_items = MagicMock()
    list_items.all.return_value = [sberbank_item]

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
        page, _resume(), "blacklist", dry_run=False, remove_employers=("Сбер",)
    )

    assert not result.success
    assert "не найден в текущем списке" in result.reason
    sberbank_delete.click.assert_not_called()


def test_remove_employer_exact_name_match_clicks_delete(monkeypatch):
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    activator = _mock_locator()
    search_input = _mock_locator()
    close = _mock_locator()

    yumoney_item, yumoney_delete = _list_item("655542", "ЮMoney")

    list_items = MagicMock()
    list_items.all.return_value = [yumoney_item]

    def _by_selector(selector):
        return {
            sel.RESUME_VISIBILITY_SAVE: save,
            **_all_mode_labels(active="blacklist"),
            sel.RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST: activator,
            sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT: search_input,
            sel.RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX: list_items,
            sel.RESUME_VISIBILITY_MODAL_CLOSE: close,
        }[selector]

    page = MagicMock()
    page.locator.side_effect = _by_selector

    result = rv.set_resume_visibility_on_hh(
        page, _resume(), "blacklist", dry_run=False, remove_employers=("ЮMoney",)
    )

    assert result.success


def test_combined_add_and_remove_clears_search_before_remove(monkeypatch):
    """Regression for the AO reviewer's PR #774 finding: without clearing the
    search field after add_employer, the already-added-employers list
    (which remove_employer reads) stays hidden behind the still-filtered
    search-results container, and remove would wrongly fail-closed with
    "не найден в текущем списке" for an employer that is actually present."""
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    activator = _mock_locator()
    search_input = _mock_locator()
    close = _mock_locator()
    confirm = _mock_locator()

    # --add-employer "Яндекс" search results.
    add_result_items = MagicMock()
    add_result_items.count.return_value = 1
    add_item = MagicMock()
    add_item.get_attribute.side_effect = lambda attr: (
        f"{sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX}1740"
        if attr == "data-qa"
        else None
    )
    add_name_locator = MagicMock()
    add_name_locator.count.return_value = 1
    add_name_locator.first.text_content.return_value = "Яндекс"
    add_item.locator.return_value = add_name_locator
    add_result_items.all.return_value = [add_item]
    add_row = MagicMock()
    add_row.count.return_value = 1
    add_row.first = add_row
    add_checkbox = MagicMock()
    add_checkbox.count.return_value = 1
    add_checkbox.first = add_checkbox
    add_row.locator.return_value = add_checkbox
    add_row_selector = (
        f"[data-qa='{sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX}1740']"
    )

    # --remove-employer "ЮMoney" — an already-added row, only visible once the
    # search field is cleared. list_items.all() is queried AFTER search.fill("")
    # in the real flow, so the mock does not need to distinguish before/after —
    # what matters is that the code reaches this branch at all, which the old
    # code (no clear) would never do (it would try to look up the search
    # field's still-filtered state, not this container).
    yumoney_item, yumoney_delete = _list_item("655542", "ЮMoney")
    list_items = MagicMock()
    list_items.all.return_value = [yumoney_item]

    def _by_selector(selector):
        return {
            sel.RESUME_VISIBILITY_SAVE: save,
            **_all_mode_labels(active="blacklist"),
            sel.RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST: activator,
            sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT: search_input,
            sel.RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX: add_result_items,
            add_row_selector: add_row,
            sel.RESUME_VISIBILITY_MODAL_CONFIRM: confirm,
            sel.RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX: list_items,
            sel.RESUME_VISIBILITY_MODAL_CLOSE: close,
        }[selector]

    page = MagicMock()
    page.locator.side_effect = _by_selector

    result = rv.set_resume_visibility_on_hh(
        page,
        _resume(),
        "blacklist",
        dry_run=False,
        add_employers=("Яндекс",),
        remove_employers=("ЮMoney",),
    )

    assert result.success
    # The search query is cleared after add, before remove reads the list.
    search_input.fill.assert_has_calls([call("Яндекс"), call("")])
    yumoney_delete.click.assert_called_once_with()
    yumoney_delete.click.assert_called_once_with()


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


def test_save_success_requires_reread_mode_match(monkeypatch):
    """#901 п.3: успех рапортуется только после перечитки экрана — checked
    внешний radio запрошенного режима и есть позитивный маркер результата
    (рапорт успеха без проверки факта — класс дефекта #899)."""
    save = _mock_locator()
    goto_calls: list[str] = []

    def _goto(_page, url):
        goto_calls.append(url)

    monkeypatch.setattr(rv, "goto_hh", _goto)

    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        **_all_mode_labels(active="no-one"),
    }[selector]

    result = rv.set_resume_visibility_on_hh(page, _resume(), "no-one", dry_run=False)

    assert result.success
    # Экран открыт дважды: до ввода и повторно после Save (перечитка).
    assert len(goto_calls) == 2


def test_save_reread_mode_mismatch_is_uncertain(monkeypatch):
    """#901 п.3: после Save перечитан ДРУГОЙ режим — пост-кликовая зона,
    uncertain (fail-closed как #176), не «success по факту клика»."""
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    mode_labels = _all_mode_labels(active="no-one")
    no_one_label = mode_labels[sel.RESUME_VISIBILITY_MODE_NO_ONE]
    link_only_label = mode_labels[sel.RESUME_VISIBILITY_MODE_LINK_ONLY]

    def _on_save_click():
        # Сервер не применил запрошенный режим: при перечитке активен link-only.
        no_one_label.locator.return_value.is_checked.return_value = False
        link_only_label.locator.return_value.is_checked.return_value = True

    save.click.side_effect = _on_save_click

    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        **mode_labels,
    }[selector]

    result = rv.set_resume_visibility_on_hh(page, _resume(), "no-one", dry_run=False)

    assert not result.success
    assert result.uncertain
    assert "ожидался «no-one»" in result.reason


def test_save_reread_mode_undefined_is_uncertain(monkeypatch):
    """Ревью PR #917: перечитка после Save не смогла определить режим (ни один
    внешний radio не checked) — пост-кликовая зона, uncertain с внятной
    причиной (без «режим «None»»). Прямой юнит-аналог browser_unit-теста
    test_read_active_mode_none_when_no_card_checked, но на уровне всей команды."""
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)
    save = _mock_locator()
    mode_labels = _all_mode_labels(active="no-one")

    def _on_save_click():
        # После сохранения ни один режим не прочитан (странная страница/дрейф).
        for label in mode_labels.values():
            label.locator.return_value.is_checked.return_value = False

    save.click.side_effect = _on_save_click

    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        **mode_labels,
    }[selector]

    result = rv.set_resume_visibility_on_hh(page, _resume(), "no-one", dry_run=False)

    assert not result.success
    assert result.uncertain
    assert "активный режим не определён" in result.reason


def test_save_reread_playwright_error_is_uncertain_not_crash(monkeypatch):
    """Перечитка после Save — пост-кликовая зона целиком: PlaywrightError из
    goto_hh (релейз после ретраев) или count()/is_checked() обязан стать
    uncertain-результатом, а не сырым исключением, обрывающим --resume all
    batch (#746 round 3 — пер-резюме гранулярность)."""
    from playwright.sync_api import Error as PlaywrightError

    save = _mock_locator()
    goto_calls: list[str] = []

    def _goto(_page, url):
        goto_calls.append(url)
        if len(goto_calls) == 2:  # перечитка после Save, не первичное открытие
            raise PlaywrightError("net::ERR_NETWORK_CHANGED")

    monkeypatch.setattr(rv, "goto_hh", _goto)

    page = MagicMock()
    page.locator.side_effect = lambda selector: {
        sel.RESUME_VISIBILITY_SAVE: save,
        **_all_mode_labels(active="no-one"),
    }[selector]

    result = rv.set_resume_visibility_on_hh(page, _resume(), "no-one", dry_run=False)

    assert not result.success
    assert result.uncertain
    assert "перечитки режима" in result.reason

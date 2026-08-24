"""Characterization tests for optional profile enrichment during login."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot import account_profile
from hhru_bot.history import History
from hhru_bot.selector_groups import account_profile as profile_selectors

pytestmark = pytest.mark.integration


def _page_for(*, counts: dict[str, int], values: dict[str, str] | None = None):
    values = values or {}
    page = MagicMock(name="Page")

    def locator(selector: str):
        loc = MagicMock(name=selector)
        loc.count.return_value = counts.get(selector, 0)
        loc.inner_text.return_value = values.get(selector, "")
        return loc

    page.locator.side_effect = locator
    return page


def test_read_account_profile_persists_only_confirmed_fields(monkeypatch, tmp_path):
    selectors = profile_selectors
    page = _page_for(
        counts={
            selectors.ACCOUNT_PROFILE_FIRST_NAME: 1,
            selectors.ACCOUNT_PROFILE_LAST_NAME: 2,
            selectors.ACCOUNT_PROFILE_PHONE: 1,
        },
        values={
            selectors.ACCOUNT_PROFILE_FIRST_NAME: "Анна",
            selectors.ACCOUNT_PROFILE_PHONE: " +7 900 ",
        },
    )
    goto = MagicMock()
    monkeypatch.setattr(account_profile, "goto_hh", goto)

    assert account_profile.read_account_profile(page, tmp_path / "history.db") == 2
    goto.assert_called_once_with(
        page,
        "https://hh.ru/profile/me",
        ready_selector=account_profile._PROFILE_READY_SELECTOR,
    )

    fields = History(tmp_path / "history.db").list_profile_fields()
    assert {(field["question_key"], field["value"]) for field in fields} == {
        ("имя", "Анна"),
        ("телефон", "+7 900"),
    }


def test_read_account_profile_is_fail_open_when_page_unavailable(monkeypatch, tmp_path, capsys):
    page = MagicMock(name="Page")
    monkeypatch.setattr(account_profile, "goto_hh", MagicMock(side_effect=PlaywrightError("down")))

    assert account_profile.read_account_profile(page, tmp_path / "history.db") == 0
    assert "[WARN] Профиль: страница недоступна" in capsys.readouterr().out
    assert not (tmp_path / "history.db").exists()


def test_read_account_profile_preserves_values_when_hydration_never_recovers(monkeypatch, tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_profile_field("Телефон", "+7", source="hh_ru")
    page = MagicMock(name="Page")
    monkeypatch.setattr(
        account_profile,
        "goto_hh",
        MagicMock(side_effect=PlaywrightError("profile marker did not appear")),
    )

    assert account_profile.read_account_profile(page, tmp_path / "history.db") == 0
    assert history.get_profile_answers() == {"телефон": "+7"}
    page.locator.assert_not_called()


def test_read_account_profile_does_not_write_empty_or_ambiguous_values(
    monkeypatch, tmp_path, capsys
):
    page = _page_for(
        counts={
            profile_selectors.ACCOUNT_PROFILE_FIRST_NAME: 1,
            profile_selectors.ACCOUNT_PROFILE_LAST_NAME: 2,
        },
        values={profile_selectors.ACCOUNT_PROFILE_FIRST_NAME: "  "},
    )
    monkeypatch.setattr(account_profile, "goto_hh", MagicMock())

    assert account_profile.read_account_profile(page, tmp_path / "history.db") == 0
    assert History(tmp_path / "history.db").list_profile_fields() == []
    output = capsys.readouterr().out
    assert "поле «Имя» пустое" in output
    assert "поле «Фамилия» не подтверждено" in output


def test_read_account_profile_removes_stale_hh_values_on_confirmed_absence(monkeypatch, tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_profile_field("Город", "Старый город", source="hh_ru")
    page = _page_for(
        counts={profile_selectors.ACCOUNT_PROFILE_FIRST_NAME: 1},
        values={profile_selectors.ACCOUNT_PROFILE_FIRST_NAME: "Новое имя"},
    )
    monkeypatch.setattr(account_profile, "goto_hh", MagicMock())

    assert account_profile.read_account_profile(page, tmp_path / "history.db") == 1
    assert history.get_profile_answers() == {"имя": "Новое имя"}


def test_read_account_profile_preserves_values_when_page_marker_is_missing(monkeypatch, tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_profile_field("Телефон", "+7", source="hh_ru")
    page = _page_for(counts={})
    monkeypatch.setattr(account_profile, "goto_hh", MagicMock())

    assert account_profile.read_account_profile(page, tmp_path / "history.db") == 0
    assert history.get_profile_answers() == {"телефон": "+7"}

"""Pure safety and LLM-contract tests for issue #265."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import hhru_bot.languages as languages
from hhru_bot.languages import (
    CEFR_LABELS,
    Language,
    build_languages_prompt,
    edit_languages_on_hh,
    parse_language_plan,
    parse_manual_languages,
)

pytestmark = pytest.mark.unit


def test_llm_must_leave_cefr_for_user_confirmation() -> None:
    assert parse_language_plan('[{"name":"English","level":null}]') == (Language("English"),)
    with pytest.raises(ValueError, match="только поля"):
        parse_language_plan('[{"name":"English"}]')


def test_llm_rejects_unknown_cefr_and_duplicates() -> None:
    with pytest.raises(ValueError, match="уровень CEFR"):
        parse_language_plan('[{"name":"English","level":"native"}]')
    with pytest.raises(ValueError, match="дублирующийся"):
        parse_language_plan('[{"name":"English","level":null},{"name":" english ","level":null}]')


def test_manual_languages_require_explicit_cefr() -> None:
    assert parse_manual_languages(["English=b2", "German=C1"]) == (
        Language("English", "B2"),
        Language("German", "C1"),
    )
    with pytest.raises(ValueError, match="NAME=A1"):
        parse_manual_languages(["English"])


def test_prompt_forbids_level_guessing() -> None:
    prompt = build_languages_prompt("English fluent", (), "append")
    assert "ВСЕГДА должно быть null" in prompt[0]["content"]


def test_cefr_labels_match_live_hh_options() -> None:
    assert CEFR_LABELS == {
        "A1": "A1 — Начальный",
        "A2": "A2 — Элементарный",
        "B1": "B1 — Средний",
        "B2": "B2 — Средне-продвинутый",
        "C1": "C1 — Продвинутый",
        "C2": "C2 — В совершенстве",
    }


def test_dry_run_never_needs_browser() -> None:
    resume = type("Resume", (), {"resume_id": "abc"})()
    result = edit_languages_on_hh(None, resume, (Language("English"),), dry_run=True, mode="append")
    assert result.success
    assert result.acted is False


def test_write_navigates_to_profile_not_resume_page(monkeypatch) -> None:
    """Languages are a profile-level entity (#265): /resume/{id} never renders
    the languages block, only /applicant/profile/me does (confirmed live on an
    empty draft and on a published resume with real language data)."""
    resume = type("Resume", (), {"resume_id": "abc", "id": "abc"})()
    page = MagicMock()
    page.url = "https://hh.ru/applicant/profile/me"
    card = MagicMock()
    card.count.return_value = 1
    card.locator.return_value.all.return_value = []
    page.locator.return_value = card

    calls = []
    monkeypatch.setattr(languages, "goto_hh", lambda _page, url: calls.append(url))
    monkeypatch.setattr(languages, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(languages, "has_login_form", lambda _page: False)

    result = edit_languages_on_hh(page, resume, (), dry_run=False, mode="append")

    assert calls == ["https://hh.ru/applicant/profile/me"]
    assert result.success


def test_existing_languages_are_read_from_cell_text_not_split_on_comma(monkeypatch) -> None:
    """The row's raw text has no separator (e.g. "РусскийРодной"); the name
    must come from the first [data-qa='cell-text'] child, not string-splitting
    the whole row."""
    resume = type("Resume", (), {"resume_id": "abc", "id": "abc"})()
    page = MagicMock()
    page.url = "https://hh.ru/applicant/profile/me"

    name_cell = MagicMock()
    name_cell.inner_text.return_value = "Русский"
    row = MagicMock()
    row.locator.return_value.first = name_cell

    card = MagicMock()
    card.count.return_value = 1
    card.locator.return_value.all.return_value = [row]
    page.locator.return_value = card

    monkeypatch.setattr(languages, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(languages, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(languages, "has_login_form", lambda _page: False)

    # "fresh" mode fails closed when the profile-level section is non-empty.
    result = edit_languages_on_hh(
        page, resume, (Language("English", "B2"),), dry_run=False, mode="fresh"
    )

    assert not result.success
    assert "пустого раздела" in result.reason

"""Pure safety and LLM-contract tests for issue #265."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import hhru_bot.languages as languages
from hhru_bot.languages import (
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
    with pytest.raises(ValueError, match="level должно быть null"):
        parse_language_plan('[{"name":"English","level":"native"}]')
    with pytest.raises(ValueError, match="дублирующийся"):
        parse_language_plan('[{"name":"English","level":null},{"name":" english ","level":null}]')


def test_llm_cannot_smuggle_a_guessed_but_valid_cefr_level() -> None:
    """#265 code-review round 1 (Codex): a syntactically valid CEFR value from
    the LLM must still be rejected outright — parse_language_plan is not the
    place a concrete level is allowed to enter, regardless of whether it looks
    like a real CEFR code. Only parse_manual_languages (operator-supplied
    NAME=CEFR) may produce a non-null Language.level."""
    with pytest.raises(ValueError, match="level должно быть null"):
        parse_language_plan('[{"name":"English","level":"B2"}]')


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


class _StrictLastLocator:
    """A stand-in whose ``.last`` is a real property, like Playwright's
    ``Locator.last`` — calling it (``.last()``) raises ``TypeError`` exactly
    as it would against the live API, unlike a permissive ``MagicMock``
    (#265 code-review round 1: ``.last()`` was a copy-paste slip that crashed
    every live language add and this test class is what catches it)."""

    def __init__(self, dialog: MagicMock) -> None:
        self._dialog = dialog

    @property
    def last(self) -> MagicMock:
        return self._dialog


def test_add_language_flow_uses_last_as_a_property_not_a_call(monkeypatch) -> None:
    """#265 code-review round 1 (Codex/claude): page.get_by_role(...).last()
    called Locator.last as a method; it is a property on the real API and
    the call form raises TypeError, uncaught by
    except (PlaywrightError, RuntimeError). This test fails on that
    regression by making get_by_role return a strict-property locator."""
    resume = type("Resume", (), {"resume_id": "abc", "id": "abc"})()
    page = MagicMock()
    page.url = "https://hh.ru/applicant/profile/me"

    card = MagicMock()
    card.count.return_value = 1
    card.locator.return_value.all.return_value = []
    add_button = MagicMock()
    add_button.count.return_value = 1

    def locator_side_effect(selector):
        from hhru_bot.selector_groups import resume_page

        if selector == resume_page.RESUME_LANGUAGE_CARD:
            return card
        if selector == resume_page.RESUME_LANGUAGE_ADD_BUTTON:
            return add_button
        return MagicMock()

    page.locator.side_effect = locator_side_effect

    dialog = MagicMock()
    form = MagicMock()
    # dialog.locator(...) backs the add-form lookup (form itself), then
    # form.locator(...) backs the language/degree select lookups, and
    # dialog.locator(...) again backs the degree-option and save-button
    # lookups in _choose_degree/_save_language — the same mocks with
    # count()==1 satisfy every strict `.count() != 1` guard along the path.
    form.locator.return_value.count.return_value = 1
    form.count.return_value = 1
    dialog.locator.return_value = form
    page.get_by_role.return_value = _StrictLastLocator(dialog)

    monkeypatch.setattr(languages, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(languages, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(languages, "has_login_form", lambda _page: False)

    result = edit_languages_on_hh(
        page, resume, (Language("English", "B2"),), dry_run=False, mode="append"
    )

    # A TypeError from a stray .last() would be swallowed by the module's
    # broad except only if it were (PlaywrightError, RuntimeError) — it isn't,
    # so a regression here surfaces as an uncaught TypeError failing the test,
    # not as a quiet result.success is False.
    assert result.success
    assert result.acted is True


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

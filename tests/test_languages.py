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
    called Locator.last as a method; it is a property on the real API, and
    against the live API the call form raises TypeError. A permissive
    MagicMock would not reproduce that TypeError (calling a MagicMock
    attribute just returns another MagicMock), so this test instead uses
    _StrictLastLocator, whose ``.last`` is a real Python property: calling it
    behaves like the live API and breaks the mocked call chain below,
    surfacing as a failed/incorrect LanguagesResult rather than a silent
    pass — see the sanity check for this test in the round-1 commit message.
    """
    resume = type("Resume", (), {"resume_id": "abc", "id": "abc"})()
    page = MagicMock()
    page.url = "https://hh.ru/applicant/profile/me"

    card = MagicMock()
    card.count.return_value = 1
    # Before the add: no rows. After add_button.click() the post-save
    # reconciliation re-reads the card (#265 round 2, Codex) and must find
    # the newly added language's name among the rows, or the write is
    # reported as unconfirmed rather than success — so the second read
    # returns a row for "English".
    added_row = MagicMock()
    added_row.locator.return_value.first.inner_text.return_value = "English"
    card.locator.return_value.all.side_effect = [[], [added_row]]
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

    assert result.success
    assert result.acted is True


def test_unconfirmed_level_aborts_before_any_click(monkeypatch) -> None:
    """#265 code-review round 2 (Codex/claude): the level==None guard must run
    before the write loop starts, not inside it. Two additions, the second
    missing a confirmed level: the first must NOT be clicked/saved on hh.ru
    before the whole call fails — a no-op (acted=False), not a partial write
    hidden behind success=False."""
    resume = type("Resume", (), {"resume_id": "abc", "id": "abc"})()
    page = MagicMock()
    page.url = "https://hh.ru/applicant/profile/me"

    card = MagicMock()
    card.count.return_value = 1
    card.locator.return_value.all.return_value = []
    add_button = MagicMock()
    add_button.count.return_value = 1
    page.locator.side_effect = lambda selector: (
        card
        if "language-card" in selector
        else add_button
        if "language-add" in selector
        else MagicMock()
    )

    monkeypatch.setattr(languages, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(languages, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(languages, "has_login_form", lambda _page: False)

    result = edit_languages_on_hh(
        page,
        resume,
        (Language("English", "B2"), Language("German", None)),
        dry_run=False,
        mode="append",
    )

    assert not result.success
    assert result.acted is False
    assert "German" in result.reason
    add_button.click.assert_not_called()


def test_dialog_hidden_is_not_trusted_as_proof_of_persistence(monkeypatch) -> None:
    """#265 code-review round 2 (Codex): the dialog closing (wait_for hidden)
    is not proof the row was actually saved server-side. If the re-read of
    the card after save doesn't show the language, the result must be a
    failure with acted=True (write attempted, outcome unconfirmed) — not a
    silent success."""
    resume = type("Resume", (), {"resume_id": "abc", "id": "abc"})()
    page = MagicMock()
    page.url = "https://hh.ru/applicant/profile/me"

    card = MagicMock()
    card.count.return_value = 1
    # The card never shows the new row, even after the dialog closes —
    # simulates a rerender/optimistic-close that didn't actually persist.
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

    assert not result.success
    assert result.acted is True
    assert "не подтверждено" in result.reason


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

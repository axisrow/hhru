"""Pure safety and LLM-contract tests for issue #265."""

from __future__ import annotations

import pytest

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

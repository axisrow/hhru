"""Characterization tests for the reference feedback-context flow (#592)."""

import pytest

from hhru_bot.ai.feedback import (
    FEEDBACK_RECENT_TAIL,
    REJECT_CONTEXT_MAX_ITEMS,
    STYLE_CONTEXT_MAX_ITEMS,
    build_reject_context,
    build_style_context,
)
from hhru_bot.ai.letters import _build_prompt
from hhru_bot.config_sections.ai_profile import AIProfile
from hhru_bot.scoring.vacancy import _build_scoring_prompt
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


def _card() -> VacancyCard:
    return VacancyCard("42", "AI Engineer", "Acme", "https://hh.ru/vacancy/42")


def _row(i: int, **overrides) -> dict:
    row = {
        "action": "reject",
        "reason": f"reason-{i}",
        "edited_snippet": f"snippet-{i}",
    }
    row.update(overrides)
    return row


def test_empty_feedback_preserves_existing_prompt_structure():
    profile = AIProfile(summary="Engineer")
    assert _build_prompt(_card(), profile) == _build_prompt(_card(), profile, [])
    assert _build_scoring_prompt(_card(), profile) == _build_scoring_prompt(_card(), profile, [])


def test_reject_context_uses_only_valid_reject_reasons():
    rows = [
        _row(1),
        _row(2, action="approve"),
        _row(3, reason="  "),
        _row(4, reason=None),
        _row(5, reason=123),
    ]
    context = build_reject_context(rows)
    assert "reason-1" in context
    assert "reason-2" not in context
    assert "reason-3" not in context
    assert "123" not in context


def test_contexts_use_reference_recent_limits_and_stable_order():
    rows = [_row(i) for i in range(FEEDBACK_RECENT_TAIL + 5)]  # newest first
    rejects = build_reject_context(rows)
    styles = build_style_context(rows)

    assert rejects.count("\n- ") == REJECT_CONTEXT_MAX_ITEMS
    assert rejects.index("reason-11") < rejects.index("reason-0")
    assert "reason-12" not in rejects
    assert styles.count("\n- ") == STYLE_CONTEXT_MAX_ITEMS
    assert styles.index("snippet-5") < styles.index("snippet-0")
    assert "snippet-6" not in styles


def test_context_budget_is_shared_constant_and_deterministic():
    rows = [_row(1, reason="r" * 100, edited_snippet="s" * 100)]
    assert len(build_reject_context(rows, max_chars=90)) <= 90
    assert len(build_style_context(rows, max_chars=110)) <= 110
    assert build_reject_context(rows, max_chars=90) == build_reject_context(rows, max_chars=90)


def test_scoring_gets_rejects_but_not_style_snippets():
    content = "\n".join(
        message["content"] for message in _build_scoring_prompt(_card(), None, [_row(1)])
    )
    assert "reason-1" in content
    assert "snippet-1" not in content


def test_letters_get_style_snippets_but_not_rejects_after_static_examples():
    profile = AIProfile(cover_letter_examples=["static-example"])
    messages = _build_prompt(_card(), profile, [_row(1)])
    content = "\n".join(message["content"] for message in messages)
    assert "snippet-1" in content
    assert "reason-1" not in content
    assert next(i for i, m in enumerate(messages) if "static-example" in m["content"]) < next(
        i for i, m in enumerate(messages) if "snippet-1" in m["content"]
    )

"""Unit tests for the keyword resolver + template model (#482).

Pure functions only -- no Playwright, no real LLM. The resolver is
independent of AI: ``resolve_by_keyword``/``resolve_static_answer`` must work
with ``llm=None``.
"""

from __future__ import annotations

import pytest

from hhru_bot.apply.questionnaire_resolver import (
    Template,
    is_denied_answer_field,
    resolve_answer,
    resolve_by_keyword,
)

pytestmark = pytest.mark.unit


class _LLM:
    """Minimal double mirroring ``ai.llm_client.LLMClient.chat``."""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages, **_kwargs):
        self.calls.append(messages)
        from hhru_bot.ai.types import NormalizedResponse

        return NormalizedResponse(content=self.content, tool_calls=None, finish_reason="stop")


# --- resolve_by_keyword -----------------------------------------------------


def test_resolve_by_keyword_matches_confirmed_normalized_text():
    confirmed = {"желаемая зарплата": "salary"}
    assert resolve_by_keyword("  Желаемая   Зарплата  ", confirmed) == "salary"


def test_resolve_by_keyword_no_match_returns_none():
    assert resolve_by_keyword("Ваш опыт работы?", {"желаемая зарплата": "salary"}) is None


def test_resolve_by_keyword_empty_confirmed_matches_returns_none():
    assert resolve_by_keyword("Любой вопрос", {}) is None


# --- resolve_answer: static templates ---------------------------------------


def test_resolve_answer_static_uses_resume_override_over_account():
    template = Template(name="salary", mode="static")
    proposal = resolve_answer(
        template,
        resume_answers={"salary": "300000"},
        account_answers={"salary": "250000"},
    )
    assert proposal is not None
    assert proposal.answer == "300000"
    assert proposal.confidence == 1.0
    assert proposal.answer_source == "template"
    assert proposal.template == "salary"


def test_resolve_answer_static_falls_back_to_account_answer():
    template = Template(name="salary", mode="static")
    proposal = resolve_answer(template, resume_answers={}, account_answers={"salary": "250000"})
    assert proposal is not None
    assert proposal.answer == "250000"


def test_resolve_answer_static_without_any_stored_value_returns_none():
    template = Template(name="salary", mode="static")
    proposal = resolve_answer(template, resume_answers={}, account_answers={})
    assert proposal is None


def test_resolve_answer_static_ignores_llm_entirely():
    """Keyword resolver must work without any AI dependency (issue #482)."""
    template = Template(name="salary", mode="static")
    proposal = resolve_answer(
        template, resume_answers={}, account_answers={"salary": "250000"}, llm=None
    )
    assert proposal is not None
    assert proposal.answer == "250000"


# --- resolve_answer: contextual templates -----------------------------------


def test_resolve_answer_contextual_without_llm_returns_none():
    """Contextual templates need an LLM; no silent low-confidence guess."""
    template = Template(
        name="motivation",
        mode="contextual",
        instruction="Explain motivation briefly",
        examples=("Пример 1",),
    )
    assert resolve_answer(template, resume_answers={}, account_answers={}, llm=None) is None


def test_resolve_answer_contextual_uses_llm_and_validates_confidence():
    template = Template(
        name="motivation",
        mode="contextual",
        instruction="Explain motivation briefly",
        examples=("Пример 1",),
    )
    llm = _LLM('{"answer": "Хочу расти в компании", "confidence": 0.95}')
    proposal = resolve_answer(
        template,
        resume_answers={},
        account_answers={},
        llm=llm,
        answer_threshold=0.90,
    )
    assert proposal is not None
    assert proposal.answer == "Хочу расти в компании"
    assert proposal.confidence == 0.95
    assert proposal.answer_source == "template"
    assert proposal.template == "motivation"
    assert llm.calls, "LLM должен был получить промпт с instruction/examples"


def test_resolve_answer_contextual_below_threshold_returns_low_confidence_proposal():
    template = Template(name="motivation", mode="contextual", instruction="...")
    llm = _LLM('{"answer": "Может быть", "confidence": 0.5}')
    proposal = resolve_answer(
        template, resume_answers={}, account_answers={}, llm=llm, answer_threshold=0.90
    )
    # Не молчаливый None: понижаем уверенность, чтобы вызывающий код мог
    # решить (skip/pending), а не терял факт попытки.
    assert proposal is not None
    assert proposal.confidence == 0.5
    assert proposal.low_confidence


def test_resolve_answer_contextual_malformed_json_is_low_confidence_not_exception():
    template = Template(name="motivation", mode="contextual", instruction="...")
    llm = _LLM("не json вовсе")
    proposal = resolve_answer(template, resume_answers={}, account_answers={}, llm=llm)
    assert proposal is not None
    assert proposal.confidence == 0.0
    assert proposal.low_confidence


# --- documents/compliance denylist ------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Номер паспорта", "passport number", "СНИЛС", "Ваш ИНН"],
)
def test_denied_fields_never_auto_answered(text):
    assert is_denied_answer_field(text) is True


def test_ordinary_field_is_not_denied():
    assert is_denied_answer_field("Желаемая зарплата") is False


def test_resolve_answer_static_refuses_denied_template_name():
    """Даже подтверждённый static-шаблон не должен отвечать на комплаенс-поля."""
    template = Template(name="Номер паспорта", mode="static")
    proposal = resolve_answer(
        template, resume_answers={}, account_answers={"Номер паспорта": "1234 567890"}
    )
    assert proposal is None

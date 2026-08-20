from __future__ import annotations

import json

import pytest

from hhru_bot.ai.questions import AIQuestionAnswerer, Question
from hhru_bot.ai.types import NormalizedResponse

pytestmark = pytest.mark.unit


class _LLM:
    def __init__(self, payload):
        self.payload = payload
        self.messages = []

    def chat(self, messages, **_kwargs):
        self.messages.append(messages)
        return NormalizedResponse(
            content=json.dumps(self.payload), tool_calls=None, finish_reason="stop"
        )


def test_exact_account_profile_answer_skips_llm_matching():
    llm = _LLM({"answer": "generated", "confidence": 1.0})
    question = Question(0, "Ваш телефон?", "text")

    proposal = AIQuestionAnswerer(llm, known_data={"Ваш телефон?": "+7 900"}).propose(question)

    assert proposal.answer == "+7 900"
    assert proposal.confidence == 1.0
    assert llm.messages == []


def test_blank_account_profile_answer_does_not_get_confidence_one():
    llm = _LLM({"answer": "generated", "confidence": 0.9})
    question = Question(0, "Ваш телефон?", "text")

    proposal = AIQuestionAnswerer(llm, known_data={"Ваш телефон?": "  "}).propose(question)

    assert proposal.answer == "generated"
    assert proposal.confidence == 0.9
    assert len(llm.messages) == 2


def test_account_profile_semantic_match_precedes_generated_answer():
    llm = _LLM({"key": "город", "confidence": 0.99})
    question = Question(0, "В каком городе вы живёте?", "text")

    proposal = AIQuestionAnswerer(llm, known_data={"город": "Москва"}).propose(question)

    assert proposal.answer == "Москва"
    assert proposal.confidence == 1.0
    assert len(llm.messages) == 1


def test_choice_answer_uses_zero_based_index_and_confidence():
    llm = _LLM({"answer": "Python", "confidence": 0.91, "indices": [1]})
    question = Question(0, "Опыт с Python?", "choice", ("Нет", "Да"))

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.option_indices == (1,)
    assert proposal.low_confidence is False
    assert "Опыт с Python?" in llm.messages[0][1]["content"]


def test_malformed_or_low_confidence_answer_is_fail_closed():
    llm = _LLM({"answer": "", "confidence": 0.2, "indices": []})
    question = Question(0, "Расскажите об опыте", "text")

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.low_confidence is True
    assert proposal.answer == ""


def test_out_of_range_choice_is_low_confidence():
    llm = _LLM({"answer": "?", "confidence": 0.99, "indices": [4]})
    question = Question(0, "Выбор", "choice", ("A", "B"))

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.low_confidence is True


def test_multiple_indices_for_radio_question_is_low_confidence():
    """codex review #373 (P1): a radio group allows exactly ONE selection.
    Before this guard, kind="choice" treated radio and checkbox identically —
    the prompt/validation allowed "one or several indices" for both — so a
    model returning multiple indices for a radio question passed validation;
    apply() then checked them sequentially and the browser kept only the
    LAST radio, silently submitting a different answer than what was
    proposed/logged/previewed in dry-run."""
    llm = _LLM({"answer": "?", "confidence": 0.95, "indices": [0, 1]})
    question = Question(0, "Выбор", "choice", ("A", "B"), is_radio=True)

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.low_confidence is True


def test_single_index_for_radio_question_is_accepted():
    """Regression guard: the is_radio check must not reject the normal case."""
    llm = _LLM({"answer": "A", "confidence": 0.95, "indices": [0]})
    question = Question(0, "Выбор", "choice", ("A", "B"), is_radio=True)

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.low_confidence is False
    assert proposal.option_indices == (0,)


def test_multiple_indices_for_checkbox_question_is_accepted():
    """Regression guard: checkboxes still allow several selections."""
    llm = _LLM({"answer": "A, B", "confidence": 0.95, "indices": [0, 1]})
    question = Question(0, "Выбор", "choice", ("A", "B"), is_radio=False)

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.low_confidence is False
    assert proposal.option_indices == (0, 1)


def test_nan_confidence_is_fail_closed_not_high_confidence():
    """codex review #373 (P1): float() accepts the JSON literal NaN without
    raising, and `nan < CONFIDENCE_THRESHOLD` is False in Python — a
    malformed confidence would read as HIGH-confidence and could be filled/
    submitted under --force instead of being skipped."""
    llm = _LLM({"answer": "5 лет", "confidence": float("nan"), "indices": []})
    question = Question(0, "Опыт", "text")

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.low_confidence is True
    assert proposal.answer == ""


def test_out_of_range_confidence_is_fail_closed():
    llm = _LLM({"answer": "5 лет", "confidence": 1.5, "indices": []})
    question = Question(0, "Опыт", "text")

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.low_confidence is True
    assert proposal.answer == ""


def test_negative_confidence_is_fail_closed():
    llm = _LLM({"answer": "5 лет", "confidence": -0.1, "indices": []})
    question = Question(0, "Опыт", "text")

    proposal = AIQuestionAnswerer(llm).propose(question)

    assert proposal.low_confidence is True
    assert proposal.answer == ""

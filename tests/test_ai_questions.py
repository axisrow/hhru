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

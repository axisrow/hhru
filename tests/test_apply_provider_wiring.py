"""Tests for _build_question_answerer's gating (issue #482).

Critical regression per the approved plan: with neither `ai.answer_questions`
nor `questionnaires.enabled` set, apply's provider wiring must stay
byte-identical to before #482 (returns the plain AIQuestionAnswerer path, or
None). `questionnaires.enabled` alone (no `ai.answer_questions`) must still
build a working keyword-only answerer -- "Keyword resolver работает без
AI-зависимости".
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hhru_bot.ai.questions import AIQuestionAnswerer
from hhru_bot.apply.questionnaire_answerer import HybridQuestionAnswerer
from hhru_bot.commands._common import _build_question_answerer
from hhru_bot.config import ResumeConfig as _RealResumeConfig
from hhru_bot.config import SearchFilters
from hhru_bot.config_sections.ai import AiConfig
from hhru_bot.config_sections.questionnaires import QuestionnairesConfig
from hhru_bot.history import History

pytestmark = pytest.mark.unit


@dataclass
class _Config:
    ai: AiConfig | None = None
    questionnaires: QuestionnairesConfig | None = None


def _resume() -> _RealResumeConfig:
    return _RealResumeConfig(
        id="backend",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python"),
    )


def test_neither_section_enabled_returns_none(tmp_path):
    history = History(tmp_path / "history.db")
    assert _build_question_answerer(_Config(), _resume(), history) is None


def test_only_ai_answer_questions_returns_plain_ai_question_answerer(tmp_path, monkeypatch):
    """Regression: byte-identical to pre-#482 behaviour when questionnaires is unset."""
    history = History(tmp_path / "history.db")
    config = _Config(ai=AiConfig(answer_questions=True))

    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient.__init__", lambda self, ai_config: None)
    answerer = _build_question_answerer(config, _resume(), history)
    assert type(answerer) is AIQuestionAnswerer
    assert not isinstance(answerer, HybridQuestionAnswerer)


def test_only_questionnaires_enabled_builds_hybrid_answerer_without_llm(tmp_path):
    history = History(tmp_path / "history.db")
    config = _Config(questionnaires=QuestionnairesConfig(enabled=True))

    answerer = _build_question_answerer(config, _resume(), history)
    assert isinstance(answerer, HybridQuestionAnswerer)


def test_both_sections_enabled_builds_hybrid_answerer_with_llm_fallback(tmp_path, monkeypatch):
    history = History(tmp_path / "history.db")
    config = _Config(
        ai=AiConfig(answer_questions=True),
        questionnaires=QuestionnairesConfig(enabled=True),
    )
    monkeypatch.setattr("hhru_bot.ai.llm_client.LLMClient.__init__", lambda self, ai_config: None)

    answerer = _build_question_answerer(config, _resume(), history)
    assert isinstance(answerer, HybridQuestionAnswerer)


def test_questionnaires_enabled_resolves_confirmed_template_end_to_end(tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_template("salary", mode="static")
    history.set_template_answer("salary", "300000")
    history.confirm_match("желаемая зарплата", "salary")
    config = _Config(questionnaires=QuestionnairesConfig(enabled=True))

    answerer = _build_question_answerer(config, _resume(), history)
    from hhru_bot.ai.questions import Question

    (proposal,) = answerer.propose_all([Question(0, "Желаемая зарплата", "text")])
    assert proposal.answer == "300000"
    assert proposal.answer_source == "template"

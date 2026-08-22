"""Tests for the resolver-chain object wired into apply/pipeline (#482).

``HybridQuestionAnswerer`` exposes the same surface as ``AIQuestionAnswerer``
(``propose_all``/``apply``) so ``pipeline.py`` needs no changes: order is
keyword resolver (static/contextual template) -> existing AIQuestionAnswerer
(profile/LLM), when configured -> low-confidence/unresolved.
"""

from __future__ import annotations

import pytest

from hhru_bot.ai.questions import AIQuestionAnswerer, Question
from hhru_bot.apply.questionnaire_answerer import HybridQuestionAnswerer
from hhru_bot.apply.questionnaire_resolver import Template

pytestmark = pytest.mark.unit


class _History:
    """Minimal double for the History methods the resolver reads."""

    def __init__(self, templates=None, answers=None, confirmed=None):
        self._templates = templates or {}
        self._answers = answers or {}
        self._confirmed = confirmed or {}

    def get_template(self, name):
        return self._templates.get(name)

    def get_template_answers(self, name):
        return self._answers.get(name, {"account": None, "resume": {}})

    def get_confirmed_matches(self):
        return self._confirmed

    def list_templates(self):
        return list(self._templates.values())


def test_keyword_resolver_answers_without_any_llm_configured():
    """Issue #482: keyword resolver works with no AI dependency at all."""
    history = _History(
        templates={"salary": Template(name="salary", mode="static")},
        answers={"salary": {"account": "300000", "resume": {}}},
        confirmed={"желаемая зарплата": "salary"},
    )
    answerer = HybridQuestionAnswerer(history=history, resume_id="backend", llm_answerer=None)
    question = Question(0, "Желаемая зарплата", "text")

    (proposal,) = answerer.propose_all([question])

    assert proposal.answer == "300000"
    assert proposal.answer_source == "template"
    assert proposal.template == "salary"
    assert not proposal.low_confidence


def test_resume_override_takes_priority_over_account_answer():
    history = _History(
        templates={"salary": Template(name="salary", mode="static")},
        answers={"salary": {"account": "250000", "resume": {"backend": "300000"}}},
        confirmed={"желаемая зарплата": "salary"},
    )
    answerer = HybridQuestionAnswerer(history=history, resume_id="backend", llm_answerer=None)
    (proposal,) = answerer.propose_all([Question(0, "Желаемая зарплата", "text")])
    assert proposal.answer == "300000"


def test_resume_override_used_when_account_answer_is_empty_string():
    """Advisor review: an empty-string account answer must not shadow a real
    resume-scoped override -- ``"" or resume_value`` must resolve to
    resume_value, not silently fail.
    """
    history = _History(
        templates={"salary": Template(name="salary", mode="static")},
        answers={"salary": {"account": "", "resume": {"backend": "300000"}}},
        confirmed={"желаемая зарплата": "salary"},
    )
    answerer = HybridQuestionAnswerer(history=history, resume_id="backend", llm_answerer=None)
    (proposal,) = answerer.propose_all([Question(0, "Желаемая зарплата", "text")])
    assert proposal.answer == "300000"


def test_falls_back_to_llm_answerer_when_resolver_has_no_match():
    history = _History()  # no templates, no confirmed matches at all
    llm_answerer = AIQuestionAnswerer(_StubLLM(), known_data={"Ваш телефон?": "+7 900"})
    answerer = HybridQuestionAnswerer(
        history=history, resume_id="backend", llm_answerer=llm_answerer
    )
    (proposal,) = answerer.propose_all([Question(0, "Ваш телефон?", "text")])
    assert proposal.answer == "+7 900"
    assert proposal.answer_source == "profile"


def test_radio_question_with_ambiguous_stored_answer_is_not_resolved():
    """Advisor review (#373 regression class): a stored template answer that
    normalizes to MORE THAN ONE radio option must not produce a proposal --
    AIQuestionAnswerer.apply() would check() every matched index and the
    browser silently keeps only the last radio, submitting a different
    answer than logged. Two distinct-but-equal-after-normalize options are
    contrived here specifically to trigger that ambiguity.
    """
    history = _History(
        templates={"remote": Template(name="remote", mode="static")},
        answers={"remote": {"account": "Да", "resume": {}}},
        confirmed={"готовы к переезду?": "remote"},
    )
    answerer = HybridQuestionAnswerer(history=history, resume_id="backend", llm_answerer=None)
    question = Question(
        0,
        "Готовы к переезду?",
        "choice",
        options=("Да", "да "),
        is_radio=True,
    )

    (proposal,) = answerer.propose_all([question])

    assert proposal.low_confidence
    assert proposal.answer_source != "template"


def test_checkbox_question_allows_multiple_matched_options():
    """Counterpart to the radio-arity guard: checkbox (is_radio=False) must
    still accept a stored answer matching more than one option -- the #373
    guard is specific to single-select radios.
    """
    history = _History(
        templates={"skills": Template(name="skills", mode="static")},
        answers={"skills": {"account": "Python", "resume": {}}},
        confirmed={"навыки": "skills"},
    )
    answerer = HybridQuestionAnswerer(history=history, resume_id="backend", llm_answerer=None)
    question = Question(
        0,
        "Навыки",
        "choice",
        options=("Python", "python "),
        is_radio=False,
    )

    (proposal,) = answerer.propose_all([question])

    assert proposal.answer_source == "template"
    assert proposal.option_indices == (0, 1)
    assert not proposal.low_confidence


def test_no_llm_answerer_and_no_resolver_match_is_low_confidence_not_exception():
    history = _History()
    answerer = HybridQuestionAnswerer(history=history, resume_id="backend", llm_answerer=None)
    (proposal,) = answerer.propose_all([Question(0, "Случайный вопрос", "text")])
    assert proposal.low_confidence
    assert proposal.answer == ""


def test_suggest_template_returns_none_when_learn_questionnaires_disabled():
    """--learn-questionnaires is opt-in: suggest_template must be a no-op
    (and never call the LLM) unless explicitly enabled.
    """
    history = _History(templates={"salary": Template(name="salary", mode="static")})
    llm_client = _StubLLM()
    answerer = HybridQuestionAnswerer(
        history=history,
        resume_id="backend",
        llm_answerer=None,
        llm=llm_client,
        learn_questionnaires=False,
    )
    assert answerer.suggest_template(Question(0, "Q", "text")) is None


def test_suggest_template_uses_llm_when_enabled():
    class _MatchLLM:
        def chat(self, messages, **_kwargs):
            from hhru_bot.ai.types import NormalizedResponse

            return NormalizedResponse(
                content='{"template": "salary", "confidence": 0.95}',
                tool_calls=None,
                finish_reason="stop",
            )

    history = _History(templates={"salary": Template(name="salary", mode="static")})
    answerer = HybridQuestionAnswerer(
        history=history,
        resume_id="backend",
        llm_answerer=None,
        llm=_MatchLLM(),
        learn_questionnaires=True,
    )
    result = answerer.suggest_template(Question(0, "Ваши зарплатные ожидания?", "text"))
    assert result == ("salary", 0.95)


def test_apply_delegates_to_ai_question_answerer_apply(monkeypatch):
    """``apply()`` must reuse the existing, already-tested DOM-filling code."""
    calls = []
    monkeypatch.setattr(
        AIQuestionAnswerer,
        "apply",
        staticmethod(lambda page, proposals: calls.append((page, proposals)) or []),
    )
    history = _History()
    answerer = HybridQuestionAnswerer(history=history, resume_id="backend", llm_answerer=None)
    proposals = answerer.propose_all([Question(0, "Q", "text")])
    answerer.apply("PAGE", proposals)
    assert calls == [("PAGE", proposals)]


class _StubLLM:
    def chat(self, messages, **_kwargs):
        raise AssertionError("keyword-resolved question must not reach the LLM")

"""Composite-answerer: шаблоны -> LLM -> очередь (#482)."""

from __future__ import annotations

import json

import pytest

from hhru_bot.ai.questions import AnswerProposal, Question
from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.config_sections.questionnaires import QuestionnairesConfig
from hhru_bot.history import History
from hhru_bot.questionnaires.answerer import TemplateQuestionAnswerer, confirm_mapping

pytestmark = pytest.mark.unit


class _LLM:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def chat(self, _messages, **_kwargs):
        self.calls += 1
        payload = self.payloads.pop(0) if self.payloads else {}
        return NormalizedResponse(
            content=json.dumps(payload), tool_calls=None, finish_reason="stop"
        )


class _StubFallback:
    def __init__(self, proposal: AnswerProposal | None = None):
        self.proposal = proposal
        self.calls = 0

    def propose(self, question):
        self.calls += 1
        return self.proposal or AnswerProposal(question, "", 0.0)


def _settings(**overrides) -> QuestionnairesConfig:
    values = {"enabled": True, "llm_match_threshold": 0.90, "llm_answer_threshold": 0.90}
    values.update(overrides)
    return QuestionnairesConfig(**values)


def _answerer(history, **kwargs) -> TemplateQuestionAnswerer:
    kwargs.setdefault("settings", _settings())
    kwargs.setdefault("confirm_fn", lambda **_: False)
    return TemplateQuestionAnswerer(history, "r1", **kwargs)


def _text(text: str = "Какие у вас зарплатные ожидания?") -> Question:
    return Question(0, text, "text")


# --- работа без AI ----------------------------------------------------------


def test_static_template_answers_without_any_llm(tmp_path):
    """Критерий приёмки: keyword resolver работает без AI-зависимости."""
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")

    proposal = _answerer(history).propose(_text())

    assert proposal.answer == "от 250000"
    assert proposal.confidence == 1.0
    assert not proposal.low_confidence
    assert proposal.answer_source == "profile"
    assert proposal.resolver_source == "static"
    assert proposal.template == "salary"


def test_static_template_never_calls_the_llm(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    llm = _LLM({"answer": "сгенерировано", "confidence": 1.0})

    _answerer(history, llm=llm).propose(_text())

    assert llm.calls == 0


def test_resume_override_is_used_by_the_answerer(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="аккаунт")
    history.set_questionnaire_template("salary", mode="static", answer="резюме", resume_id="r1")

    assert _answerer(history).propose(_text()).answer == "резюме"


def test_confirmed_phrase_routes_a_custom_wording(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    history.confirm_questionnaire_example("salary", "Сколько денег вы хотите?")

    assert _answerer(history).propose(_text("Сколько денег вы хотите?")).answer == "от 250000"


# --- очередь ----------------------------------------------------------------


def test_unmatched_question_goes_to_the_queue(tmp_path):
    answerer = _answerer(History(tmp_path / "h.db"))

    proposal = answerer.propose(_text("Опишите свой самый сложный проект"))

    assert proposal.answer == ""
    assert proposal.low_confidence
    assert len(answerer.pending) == 1
    assert answerer.pending[0]["reason"]


def test_matched_but_unsaved_template_goes_to_the_queue(tmp_path):
    answerer = _answerer(History(tmp_path / "h.db"))

    proposal = answerer.propose(_text())

    assert proposal.low_confidence
    assert answerer.pending[0]["template"] == "salary"
    assert "нет сохранённого ответа" in answerer.pending[0]["reason"]


def test_pending_entry_carries_question_shape(tmp_path):
    answerer = _answerer(History(tmp_path / "h.db"))

    answerer.propose(Question(0, "Формат работы?", "choice", ("Офис", "Удалённо"), is_radio=True))

    item = answerer.pending[0]
    assert item["kind"] == "choice"
    assert item["is_radio"] is True
    assert item["options"] == ["Офис", "Удалённо"]


def test_pending_is_a_copy_and_survives_further_calls(tmp_path):
    answerer = _answerer(History(tmp_path / "h.db"))
    answerer.propose(_text("Первый неизвестный вопрос"))

    snapshot = answerer.pending
    answerer.propose(_text("Второй неизвестный вопрос"))

    assert len(snapshot) == 1
    assert len(answerer.pending) == 2


def test_proposal_order_matches_input(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    questions = [_text("Неизвестный вопрос"), _text()]

    proposals = _answerer(history).propose_all(questions)

    assert [p.question.text for p in proposals] == [q.text for q in questions]


# --- contextual + пороги ----------------------------------------------------


def test_contextual_template_uses_llm(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="contextual", instruction="назови вилку")
    llm = _LLM({"answer": "250-300 тысяч", "confidence": 0.97})

    proposal = _answerer(history, llm=llm).propose(_text())

    assert proposal.answer == "250-300 тысяч"
    assert proposal.answer_source == "llm"
    assert proposal.resolver_source == "contextual"
    assert llm.calls == 1


def test_configured_threshold_reaches_the_proposal(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")

    proposal = _answerer(history, settings=_settings(llm_answer_threshold=0.95)).propose(_text())

    assert proposal.threshold == pytest.approx(0.95)


def test_confidence_between_default_and_configured_threshold_is_low(tmp_path):
    """0.85 прошло бы дефолтные 0.70, но должно отсекаться настроенными 0.90."""
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="contextual", instruction="вилка")
    llm = _LLM({"answer": "возможно", "confidence": 0.85})

    proposal = _answerer(history, llm=llm).propose(_text())

    assert proposal.low_confidence
    assert "ниже порога" in _last_reason(history, llm)


def _last_reason(history, llm) -> str:
    answerer = _answerer(history, llm=_LLM({"answer": "возможно", "confidence": 0.85}))
    answerer.propose(_text())
    return answerer.pending[0]["reason"]


# --- комплаенс --------------------------------------------------------------


def test_compliance_question_without_static_stays_in_the_queue(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template(
        "work_permit", mode="contextual", cluster="compliance", instruction="ответь"
    )
    history.confirm_questionnaire_example("work_permit", "Есть ли разрешение на работу?")
    llm = _LLM({"answer": "да", "confidence": 1.0})

    proposal = _answerer(history, llm=llm).propose(_text("Есть ли разрешение на работу?"))

    assert proposal.low_confidence
    assert llm.calls == 0


def test_compliance_question_with_explicit_static_is_answered(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template(
        "work_permit", mode="static", cluster="compliance", answer="Гражданство РФ"
    )
    history.confirm_questionnaire_example("work_permit", "Есть ли разрешение на работу?")

    proposal = _answerer(history).propose(_text("Есть ли разрешение на работу?"))

    assert proposal.answer == "Гражданство РФ"
    assert not proposal.low_confidence


# --- подтверждение LLM-сопоставления ---------------------------------------


def test_llm_mapping_requires_confirmation(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    llm = _LLM({"template": "salary", "confidence": 0.99})
    asked: list[str] = []

    def _refuse(*, learn, prompt):  # noqa: ARG001 - фиксируем сам факт вопроса
        asked.append(prompt)
        return False

    answerer = _answerer(history, llm=llm, learn=True, confirm_fn=_refuse)
    proposal = answerer.propose(_text("Сколько вы хотите получать в месяц?"))

    assert asked, "пользователя должны были спросить"
    assert proposal.low_confidence
    assert "подтверждение" in answerer.pending[0]["reason"]


def test_confirmed_llm_mapping_answers_and_is_remembered(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    llm = _LLM({"template": "salary", "confidence": 0.99})

    answerer = _answerer(history, llm=llm, learn=True, confirm_fn=lambda **_: True)
    proposal = answerer.propose(_text("Сколько вы хотите получать в месяц?"))

    assert proposal.answer == "от 250000"
    assert history.get_confirmed_phrases("r1")["сколько вы хотите получать в месяц?"] == "salary"


def test_second_identical_question_is_not_asked_twice(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    llm = _LLM({"template": "salary", "confidence": 0.99})
    asks = []

    answerer = _answerer(
        history, llm=llm, learn=True, confirm_fn=lambda **kw: (asks.append(kw), True)[1]
    )
    answerer.propose(_text("Сколько вы хотите получать в месяц?"))
    answerer.propose(_text("Сколько вы хотите получать в месяц?"))

    assert len(asks) == 1


def test_compliance_is_never_mapped_by_the_llm(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template(
        "work_permit", mode="static", cluster="compliance", answer="Гражданство РФ"
    )
    llm = _LLM({"template": "work_permit", "confidence": 0.99})

    answerer = _answerer(history, llm=llm, learn=True, confirm_fn=lambda **_: True)
    proposal = answerer.propose(_text("Подтверждаете ли вы отсутствие судимости?"))

    assert proposal.low_confidence
    assert history.get_confirmed_phrases("r1") == {}


# --- LLM fallback для вопросов вне шаблонов --------------------------------


def test_fallback_answers_a_question_no_template_covers(tmp_path):
    question = _text("Опишите свой самый сложный проект")
    fallback = _StubFallback(AnswerProposal(question, "Проект X", 0.95, answer_source="llm"))

    answerer = _answerer(History(tmp_path / "h.db"), llm_fallback=fallback)
    proposal = answerer.propose(question)

    assert proposal.answer == "Проект X"
    assert proposal.resolver_source == "fallback"
    assert answerer.pending == []


def test_low_confidence_fallback_goes_to_the_queue(tmp_path):
    question = _text("Опишите свой самый сложный проект")
    fallback = _StubFallback(AnswerProposal(question, "Наверное", 0.5))

    answerer = _answerer(History(tmp_path / "h.db"), llm_fallback=fallback)

    assert answerer.propose(question).low_confidence
    assert len(answerer.pending) == 1


def test_fallback_is_not_used_for_a_matched_template(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    fallback = _StubFallback()

    _answerer(history, llm_fallback=fallback).propose(_text())

    assert fallback.calls == 0


# --- confirm_mapping --------------------------------------------------------


def test_confirm_mapping_never_asks_without_the_learn_flag():
    def _boom(_prompt):
        raise AssertionError("не должны спрашивать без --learn-questionnaires")

    assert confirm_mapping(learn=False, prompt="?", isatty_fn=lambda: True, input_fn=_boom) is False


def test_confirm_mapping_declines_without_a_tty():
    assert confirm_mapping(learn=True, prompt="?", isatty_fn=lambda: False) is False


@pytest.mark.parametrize("answer", ["y", "yes", "да", "Д", " Y "])
def test_confirm_mapping_accepts_positive_answers(answer):
    assert confirm_mapping(
        learn=True, prompt="?", isatty_fn=lambda: True, input_fn=lambda _p: answer
    )


@pytest.mark.parametrize("answer", ["", "n", "нет", "later"])
def test_confirm_mapping_rejects_everything_else(answer):
    assert not confirm_mapping(
        learn=True, prompt="?", isatty_fn=lambda: True, input_fn=lambda _p: answer
    )


# --- комплаенс по тексту, без опоры на сопоставление (регресс) -------------


@pytest.mark.parametrize(
    "text",
    [
        "Готовы предоставить справку об отсутствии судимости?",
        "Есть ли у вас разрешение на работу в РФ?",
        "Ваше гражданство?",
        "Укажите серию и номер паспорта",
        "Есть ли действующая медицинская книжка?",
        "Ваш ИНН",
        "Состоите ли вы на воинском учёте?",
    ],
)
def test_compliance_text_is_never_answered_by_free_generation(tmp_path, text):
    """Кластерного гейта мало: он опирается на найденный шаблон, а такой вопрос
    может не совпасть ни с одним и уйти в свободную LLM-генерацию."""
    question = _text(text)
    fallback = _StubFallback(AnswerProposal(question, "Да", 0.99, answer_source="llm"))

    answerer = _answerer(History(tmp_path / "h.db"), llm_fallback=fallback)
    proposal = answerer.propose(question)

    assert proposal.answer == ""
    assert proposal.low_confidence
    assert fallback.calls == 0, "к генератору обращаться нельзя"
    assert answerer.pending, "вопрос должен попасть в очередь"


def test_compliance_text_is_not_mapped_by_the_llm_either(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    llm = _LLM({"template": "salary", "confidence": 0.99})

    answerer = _answerer(history, llm=llm, learn=True, confirm_fn=lambda **_: True)
    proposal = answerer.propose(_text("Ваше гражданство?"))

    assert proposal.low_confidence
    assert llm.calls == 0


def test_ordinary_question_still_reaches_the_fallback(tmp_path):
    """Комплаенс-паттерн не должен глушить обычные вопросы."""
    question = _text("Опишите свой самый сложный проект")
    fallback = _StubFallback(AnswerProposal(question, "Проект X", 0.95, answer_source="llm"))

    assert _answerer(History(tmp_path / "h.db"), llm_fallback=fallback).propose(question).answer


# --- llm_match_threshold реально гейтит (регресс) --------------------------


def test_llm_mapping_below_the_configured_threshold_is_rejected(tmp_path):
    """Порог из конфига должен решать, а не быть декоративным полем."""
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    llm = _LLM({"template": "salary", "confidence": 0.86})

    answerer = _answerer(
        history,
        settings=_settings(llm_match_threshold=0.99),
        llm=llm,
        learn=True,
        confirm_fn=lambda **_: True,
    )
    proposal = answerer.propose(_text("Сколько вы хотите получать в месяц?"))

    assert proposal.answer == ""
    assert proposal.low_confidence


def test_llm_mapping_above_the_threshold_is_accepted(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")
    llm = _LLM({"template": "salary", "confidence": 0.95})

    answerer = _answerer(
        history,
        settings=_settings(llm_match_threshold=0.90),
        llm=llm,
        learn=True,
        confirm_fn=lambda **_: True,
    )

    assert answerer.propose(_text("Сколько вы хотите получать в месяц?")).answer == "от 250000"


@pytest.mark.parametrize(
    "payload",
    [
        {"template": "salary", "confidence": float("nan")},
        {"template": "salary", "confidence": 1.5},
        {"template": "неизвестный", "confidence": 0.99},
        {"template": None, "confidence": 0.99},
        {"confidence": 0.99},
    ],
)
def test_malformed_llm_mapping_is_rejected(tmp_path, payload):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="от 250000")

    answerer = _answerer(history, llm=_LLM(payload), learn=True, confirm_fn=lambda **_: True)

    assert answerer.propose(_text("Сколько вы хотите получать в месяц?")).low_confidence


def test_llm_mapping_records_the_reported_confidence(tmp_path):
    """В аудит должна попадать уверенность модели, а не сам порог."""
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="contextual", instruction="вилка")
    llm = _LLM(
        {"template": "salary", "confidence": 0.93},
        {"answer": "250-300", "confidence": 0.97},
    )

    answerer = _answerer(
        history,
        settings=_settings(llm_match_threshold=0.90),
        llm=llm,
        learn=True,
        confirm_fn=lambda **_: True,
    )
    proposal = answerer.propose(_text("Сколько вы хотите получать в месяц?"))

    assert proposal.confidence == pytest.approx(0.97), "уверенность ОТВЕТА, не порог"
    assert proposal.template == "salary"

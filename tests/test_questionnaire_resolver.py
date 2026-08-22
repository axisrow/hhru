"""Чистая логика сопоставления вопрос -> шаблон -> ответ (#482).

Без браузера, без SQLite, без LLM-пакета: keyword resolver обязан работать при
отсутствующей optional-зависимости .[ai] (критерий приёмки issue).
"""

from __future__ import annotations

import json

import pytest

from hhru_bot.ai.questions import Question
from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.questionnaires.resolver import (
    KEYWORD,
    PHRASE,
    ResolvedAnswer,
    TemplateMatch,
    build_answer,
    check_choice_compatibility,
    choice_indices,
    compliance_gate,
    match_keyword,
    match_phrase,
    resolve_template,
)
from hhru_bot.questionnaires.templates import QuestionTemplate

pytestmark = pytest.mark.unit


class _LLM:
    """Минимальный двойник LLMClient: считает вызовы, отдаёт готовый payload."""

    def __init__(self, payload, *, raw: str | None = None):
        self.payload = payload
        self.raw = raw
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages, **_kwargs):
        self.calls.append(messages)
        content = self.raw if self.raw is not None else json.dumps(self.payload)
        return NormalizedResponse(content=content, tool_calls=None, finish_reason="stop")


class _BoomLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **_kwargs):
        self.calls += 1
        raise RuntimeError("транспорт недоступен")


def _choice(text: str, *options: str, is_radio: bool = True) -> Question:
    return Question(0, text, "choice", tuple(options), is_radio=is_radio)


def _text(text: str) -> Question:
    return Question(0, text, "text")


def _static(name: str, answer: str, cluster: str = "conditions") -> QuestionTemplate:
    return QuestionTemplate(name, cluster, "static", answer=answer)


def _contextual(name: str, instruction: str, *, cluster: str = "conditions", examples=()):
    return QuestionTemplate(name, cluster, "contextual", instruction=instruction, examples=examples)


# --- match_phrase -----------------------------------------------------------


def test_match_phrase_finds_confirmed_wording():
    match = match_phrase("Ваши зарплатные ожидания?", {"ваши зарплатные ожидания?": "salary"})

    assert match is not None
    assert (match.template, match.source, match.confidence) == ("salary", PHRASE, 1.0)


def test_match_phrase_ignores_case_and_spacing():
    confirmed = {"ваши зарплатные ожидания?": "salary"}

    assert match_phrase("  ВАШИ   Зарплатные\nОжидания?  ", confirmed) is not None


def test_match_phrase_returns_none_for_unknown_text():
    assert match_phrase("Расскажите о себе", {"ваш город": "location"}) is None


def test_match_phrase_carries_seed_cluster():
    match = match_phrase("вопрос", {"вопрос": "salary"})

    assert match is not None and match.cluster == "conditions"


def test_match_phrase_gives_unknown_template_the_default_cluster():
    match = match_phrase("вопрос", {"вопрос": "my_own_template"})

    assert match is not None and match.cluster == "mixed"


# --- match_keyword ----------------------------------------------------------


def test_match_keyword_resolves_salary_question():
    match = match_keyword("Какие у вас зарплатные ожидания?")

    assert match is not None
    assert (match.template, match.source) == ("salary", KEYWORD)
    assert match.confidence == pytest.approx(0.95)


def test_match_keyword_resolves_each_seed_field():
    resolved = {
        match_keyword(text).template
        for text in (
            "Ваши зарплатные ожидания?",
            "В каком городе вы живёте?",
            "Какая желаемая должность?",
            "С какими нишами вы работали?",
        )
    }

    assert resolved == {"salary", "location", "desired_role", "business_segments"}


def test_match_keyword_returns_none_without_any_hit():
    assert match_keyword("Опишите свой самый сложный проект") is None


def test_match_keyword_returns_none_when_two_templates_match():
    """Fail-closed: ответить одним значением на двусмысленный вопрос нельзя."""
    ambiguous = "В каком городе вы работаете и какие зарплатные ожидания?"

    assert match_keyword(ambiguous) is None


# --- resolve_template -------------------------------------------------------


def test_resolve_template_prefers_confirmed_phrase_over_keyword():
    text = "Какие у вас зарплатные ожидания?"

    match = resolve_template(text, confirmed={"какие у вас зарплатные ожидания?": "custom"})

    assert match is not None and match.template == "custom" and match.source == PHRASE


def test_resolve_template_falls_back_to_keyword():
    match = resolve_template("Какие у вас зарплатные ожидания?", confirmed={})

    assert match is not None and match.template == "salary" and match.source == KEYWORD


def test_resolve_template_returns_none_when_nothing_matches():
    assert resolve_template("Что вас мотивирует?", confirmed={}) is None


# --- compliance_gate --------------------------------------------------------


def _compliance_match() -> TemplateMatch:
    return TemplateMatch("work_permit", "compliance", KEYWORD, 0.95)


def test_compliance_gate_allows_non_strict_cluster_without_template():
    assert compliance_gate(None, TemplateMatch("salary", "conditions", KEYWORD, 0.95)) == ""


def test_compliance_gate_blocks_missing_template():
    assert "без сохранённого значения" in compliance_gate(None, _compliance_match())


def test_compliance_gate_blocks_contextual_template():
    template = _contextual("work_permit", "ответь по документам", cluster="compliance")

    assert "только явный static" in compliance_gate(template, _compliance_match())


def test_compliance_gate_blocks_blank_static_answer():
    template = QuestionTemplate("work_permit", "compliance", "static", answer="   ")

    assert "только явный static" in compliance_gate(template, _compliance_match())


def test_compliance_gate_allows_explicit_static_answer():
    template = _static("work_permit", "Гражданство РФ", cluster="compliance")

    assert compliance_gate(template, _compliance_match()) == ""


# --- choice_indices / check_choice_compatibility ----------------------------


def test_choice_indices_match_by_text_not_position():
    question = _choice("Формат работы?", "Офис", "Удалённо", "Гибрид")

    assert choice_indices(question, "Удалённо") == (1,)


def test_choice_indices_survive_reordered_options():
    value = "Удалённо"
    first = _choice("Формат?", "Офис", "Удалённо")
    reordered = _choice("Формат?", "Удалённо", "Офис")

    picked_first = first.options[choice_indices(first, value)[0]]
    picked_second = reordered.options[choice_indices(reordered, value)[0]]

    assert picked_first == picked_second == value


def test_choice_indices_support_multiple_values_for_checkbox():
    question = _choice("Сегменты?", "B2B", "B2C", "B2G", is_radio=False)

    assert choice_indices(question, "B2B, B2G") == (0, 2)


def test_choice_indices_ignore_case_and_spacing():
    question = _choice("Формат?", "Удалённо")

    assert choice_indices(question, "  удалённо ") == (0,)


def test_choice_indices_empty_when_nothing_matches():
    question = _choice("Формат?", "Офис", "Удалённо")

    assert choice_indices(question, "Вахтой") == ()


def test_check_choice_compatibility_accepts_single_radio_pick():
    assert check_choice_compatibility(_choice("q", "a", "b"), (0,)) == ""


def test_check_choice_compatibility_rejects_multiple_radio_picks():
    reason = check_choice_compatibility(_choice("q", "a", "b"), (0, 1))

    assert "один вариант" in reason


def test_check_choice_compatibility_accepts_multiple_checkbox_picks():
    question = _choice("q", "a", "b", is_radio=False)

    assert check_choice_compatibility(question, (0, 1)) == ""


def test_check_choice_compatibility_rejects_empty_selection():
    assert check_choice_compatibility(_choice("q", "a"), ()) != ""


# --- build_answer: static ---------------------------------------------------


def _salary_match() -> TemplateMatch:
    return TemplateMatch("salary", "conditions", KEYWORD, 0.95)


def test_build_answer_static_text_question():
    resolved = build_answer(_text("Ожидания?"), _static("salary", "от 250000"), _salary_match())

    assert resolved.resolved
    assert resolved.answer == "от 250000"
    assert resolved.confidence == 1.0
    assert resolved.answer_source == "profile"
    assert resolved.resolver_source == "static"


def test_build_answer_static_never_calls_llm():
    llm = _LLM({"answer": "generated", "confidence": 1.0})

    build_answer(_text("Ожидания?"), _static("salary", "от 250000"), _salary_match(), llm=llm)

    assert llm.calls == []


def test_build_answer_static_choice_picks_matching_option():
    question = _choice("Формат?", "Офис", "Удалённо")

    resolved = build_answer(question, _static("format", "Удалённо"), _salary_match())

    assert resolved.resolved and resolved.option_indices == (1,)


def test_build_answer_static_choice_pends_when_option_missing():
    question = _choice("Формат?", "Офис", "Гибрид")

    resolved = build_answer(question, _static("format", "Удалённо"), _salary_match())

    assert not resolved.resolved
    assert "ни один вариант" in resolved.pending_reason


def test_build_answer_pends_without_saved_template():
    resolved = build_answer(_text("Ожидания?"), None, _salary_match())

    assert not resolved.resolved
    assert "нет сохранённого ответа" in resolved.pending_reason


def test_build_answer_pends_on_invalid_template():
    broken = QuestionTemplate("salary", "conditions", "static", answer="")

    resolved = build_answer(_text("Ожидания?"), broken, _salary_match())

    assert not resolved.resolved and "непригоден" in resolved.pending_reason


# --- build_answer: contextual ----------------------------------------------


def test_build_answer_contextual_without_llm_pends():
    resolved = build_answer(_text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match())

    assert not resolved.resolved and "LLM не настроен" in resolved.pending_reason


def test_build_answer_contextual_uses_llm_and_marks_source():
    llm = _LLM({"answer": "от 250000 рублей", "confidence": 0.95})

    resolved = build_answer(
        _text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match(), llm=llm
    )

    assert resolved.resolved
    assert resolved.answer == "от 250000 рублей"
    assert resolved.answer_source == "llm"
    assert resolved.resolver_source == "contextual"


def test_contextual_prompt_carries_instruction_and_examples():
    llm = _LLM({"answer": "ответ", "confidence": 0.99})
    template = _contextual("salary", "назови вилку 250-300", examples=("Ваш желаемый доход?",))

    build_answer(_text("Ожидания?"), template, _salary_match(), llm=llm)

    prompt = llm.calls[0][1]["content"]
    assert "назови вилку 250-300" in prompt
    assert "Ваш желаемый доход?" in prompt


def test_build_answer_contextual_pends_below_threshold():
    llm = _LLM({"answer": "возможно", "confidence": 0.85})

    resolved = build_answer(
        _text("Ожидания?"),
        _contextual("salary", "скажи вилку"),
        _salary_match(),
        llm=llm,
        answer_threshold=0.90,
    )

    assert not resolved.resolved and "ниже порога" in resolved.pending_reason


def test_build_answer_contextual_accepts_confidence_at_threshold():
    llm = _LLM({"answer": "точно", "confidence": 0.90})

    resolved = build_answer(
        _text("Ожидания?"),
        _contextual("salary", "скажи вилку"),
        _salary_match(),
        llm=llm,
        answer_threshold=0.90,
    )

    assert resolved.resolved


@pytest.mark.parametrize(
    "raw",
    [
        '{"answer":"x","confidence":NaN}',
        '{"answer":"x","confidence":1.5}',
        '{"answer":"x","confidence":-0.1}',
        "не json вовсе",
        '["не", "объект"]',
        '{"answer":"","confidence":0.99}',
    ],
)
def test_build_answer_contextual_pends_on_malformed_llm_output(raw):
    llm = _LLM(None, raw=raw)

    resolved = build_answer(
        _text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match(), llm=llm
    )

    assert not resolved.resolved and resolved.pending_reason


def test_build_answer_contextual_pends_on_out_of_range_index():
    llm = _LLM({"answer": "x", "confidence": 0.99, "indices": [7]})
    question = _choice("Формат?", "Офис", "Удалённо")

    resolved = build_answer(question, _contextual("format", "выбери"), _salary_match(), llm=llm)

    assert not resolved.resolved and resolved.pending_reason


def test_build_answer_contextual_pends_on_multiple_radio_indices():
    llm = _LLM({"answer": "x", "confidence": 0.99, "indices": [0, 1]})
    question = _choice("Формат?", "Офис", "Удалённо")

    resolved = build_answer(question, _contextual("format", "выбери"), _salary_match(), llm=llm)

    assert not resolved.resolved and resolved.pending_reason


def test_build_answer_contextual_fills_answer_text_from_chosen_options():
    llm = _LLM({"answer": "", "confidence": 0.99, "indices": [1]})
    question = _choice("Формат?", "Офис", "Удалённо")

    resolved = build_answer(question, _contextual("format", "выбери"), _salary_match(), llm=llm)

    assert resolved.resolved and resolved.answer == "Удалённо"


def test_build_answer_contextual_pends_when_llm_raises():
    llm = _BoomLLM()

    resolved = build_answer(
        _text("Ожидания?"), _contextual("salary", "скажи вилку"), _salary_match(), llm=llm
    )

    assert llm.calls == 1
    assert not resolved.resolved and "LLM не дал пригодного ответа" in resolved.pending_reason


def test_compliance_contextual_never_reaches_llm():
    llm = _LLM({"answer": "да", "confidence": 1.0})
    template = _contextual("work_permit", "ответь по документам", cluster="compliance")

    resolved = build_answer(_text("Есть ли разрешение?"), template, _compliance_match(), llm=llm)

    assert llm.calls == []
    assert not resolved.resolved


def test_resolved_answer_pending_helper_is_not_resolved():
    assert not ResolvedAnswer.pending("причина").resolved

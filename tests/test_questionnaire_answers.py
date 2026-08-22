from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hhru_bot.ai.questions import Question
from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.config_sections.questionnaires import QuestionnaireConfig
from hhru_bot.history import History
from hhru_bot.questionnaire_answers import (
    QuestionnaireResolver,
    normalize_question,
    question_fingerprint,
)
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


class LLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return NormalizedResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            tool_calls=None,
            finish_reason="stop",
        )


def resolver(tmp_path, *, llm=None, known_data=None, profile=None, inputs=None):
    history = History(tmp_path / "history.db")
    answers = iter(inputs or [])
    item = QuestionnaireResolver(
        history,
        QuestionnaireConfig(enabled=True),
        llm=llm,
        known_data=known_data,
        profile=profile,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _message: None,
    )
    item.set_context(VacancyCard("1", "Python", "ACME", "https://hh.ru/vacancy/1"), "python")
    return history, item


def test_keyword_location_uses_local_profile_without_llm(tmp_path):
    history, item = resolver(tmp_path, known_data={"Город": "Москва"})

    result = item.resolve(Question(0, "В каком городе вы проживаете?", "text"))

    assert result.status == "resolved"
    assert result.answer == "Москва"
    assert result.template_key == "location"
    assert result.match_source == "keyword"
    assert history.list_questionnaire_pending() == []


def test_resume_answer_overrides_account_answer(tmp_path):
    history, item = resolver(tmp_path)
    history.set_questionnaire_answer(
        "location", mode="static", payload={"text": "Москва", "choices": []}
    )
    history.set_questionnaire_answer(
        "location",
        scope_id="python",
        mode="static",
        payload={"text": "Санкт-Петербург", "choices": []},
    )

    result = item.resolve(Question(0, "В каком городе вы проживаете?", "text"))

    assert result.answer == "Санкт-Петербург"
    assert result.answer_source == "template"


def test_keyword_choice_matches_visible_option_by_label(tmp_path):
    history, item = resolver(tmp_path)
    history.set_questionnaire_answer(
        "business_segments",
        scope_id="python",
        mode="static",
        payload={"text": "B2B", "choices": ["B2B"]},
    )
    question = Question(
        0,
        "С какими сегментами бизнеса вы работали?",
        "choice",
        ("B2B", "B2C", "B2G"),
        is_radio=True,
    )

    result = item.resolve(question)

    assert result.status == "resolved"
    assert result.choice_labels == ("B2B",)


def test_unknown_without_llm_is_deduplicated_in_pending_queue(tmp_path):
    history, item = resolver(tmp_path)
    question = Question(0, "Расскажите о самом сложном проекте", "text")

    first = item.resolve(question)
    second = item.resolve(question)

    assert first.status == second.status == "pending"
    rows = history.list_questionnaire_pending()
    assert len(rows) == 1
    assert rows[0]["seen_count"] == 2
    assert "proposal_json" not in rows[0]


def test_unresolved_question_reopens_a_previously_confirmed_pending_row(tmp_path):
    history, item = resolver(tmp_path)
    question = Question(0, "Расскажите о сложном проекте", "text")
    item.resolve(question)
    pending_id = history.list_questionnaire_pending()[0]["id"]
    history.mark_questionnaire_pending(pending_id, "confirmed")

    item.resolve(question)

    rows = history.list_questionnaire_pending()
    assert [row["id"] for row in rows] == [pending_id]
    assert rows[0]["seen_count"] == 2


def test_first_llm_mapping_requires_confirmation(tmp_path):
    llm = LLM(
        {
            "template_key": "python_experience",
            "label": "Опыт Python",
            "cluster": "expertise",
            "mode": "static",
            "scope": "resume",
            "sensitive": False,
            "instruction": "",
            "match_confidence": 0.98,
            "answer_confidence": 0.97,
            "answer": {"text": "5 лет", "choices": []},
        }
    )
    history, item = resolver(tmp_path, llm=llm)

    result = item.resolve(Question(0, "Какой у вас опыт Python?", "text"))

    assert result.status == "pending"
    assert "не подтверждено" in result.reason
    assert history.list_questionnaire_pending()[0]["proposal"]["template_key"] == (
        "python_experience"
    )


def test_first_llm_mapping_cannot_reuse_stored_answer_without_confirmation(tmp_path):
    llm = LLM(
        {
            "template_key": "location",
            "label": "Город / страна проживания",
            "cluster": "conditions",
            "mode": "static",
            "scope": "account",
            "sensitive": False,
            "instruction": "",
            "match_confidence": 0.99,
            "answer_confidence": 0.99,
            "answer": {"text": "Москва", "choices": []},
        }
    )
    history, item = resolver(tmp_path, llm=llm)
    history.set_questionnaire_answer(
        "location",
        scope_id="",
        mode="static",
        payload={"text": "Москва", "choices": []},
    )

    result = item.resolve(Question(0, "Укажите ваш населённый пункт", "text"))

    assert result.status == "pending"
    assert result.reason == "первое LLM-сопоставление не подтверждено"


def test_static_keyword_without_explicit_value_is_not_generated_by_llm(tmp_path):
    llm = LLM(
        {
            "answer": {"text": "Выдуманный город", "choices": []},
            "answer_confidence": 1.0,
        }
    )
    _history, item = resolver(tmp_path, llm=llm)

    result = item.resolve(Question(0, "В каком городе вы живёте?", "text"))

    assert result.status == "pending"
    assert result.reason == "статическое поле требует явного ответа"
    assert llm.calls == []


def test_interactive_keyword_fallback_asks_for_static_answer(tmp_path):
    history, item = resolver(tmp_path, inputs=["Москва"])
    item.set_context(
        VacancyCard("1", "Python", "ACME", "https://hh.ru/vacancy/1"),
        "python",
        interactive=True,
    )

    result = item.resolve(Question(0, "В каком городе вы живёте?", "text"))

    assert result.status == "resolved"
    assert result.answer == "Москва"
    assert result.answer_source == "user"
    stored = history.get_questionnaire_answer("location", "python")
    assert stored["payload"]["text"] == "Москва"


def test_interactive_unknown_question_can_map_and_capture_answer(tmp_path):
    history, item = resolver(
        tmp_path,
        inputs=["m", "desired_role", "Руководить продуктовой командой"],
    )
    item.set_context(
        VacancyCard("1", "Product Lead", "ACME", "https://hh.ru/vacancy/1"),
        "marketing",
        interactive=True,
    )

    result = item.resolve(Question(0, "Какую работу вы ищете?", "text"))

    assert result.status == "resolved"
    assert result.template_key == "desired_role"
    assert result.answer == "Руководить продуктовой командой"
    assert (
        history.get_questionnaire_alias(question_fingerprint("Какую работу вы ищете?", "text", ()))[
            "template_key"
        ]
        == "desired_role"
    )


def test_sensitive_template_never_generates_answer(tmp_path):
    llm = LLM({"unexpected": True})
    history, item = resolver(tmp_path, llm=llm)
    history.upsert_questionnaire_template(
        "citizenship",
        "Гражданство",
        "compliance",
        "static",
        "account",
        sensitive=True,
        source="user",
        confirmed=True,
    )
    text = "Укажите ваше гражданство"
    fingerprint = question_fingerprint(text, "text", ())
    history.upsert_questionnaire_alias(
        fingerprint,
        text,
        normalize_question(text),
        "text",
        (),
        "citizenship",
        "compliance",
        source="user",
        confirmed=True,
    )

    result = item.resolve(Question(0, text, "text"))

    assert result.status == "pending"
    assert "явного ответа" in result.reason
    assert llm.calls == []


def test_desired_role_can_be_inferred_from_resume_profile(tmp_path):
    profile = SimpleNamespace(
        summary="Backend developer",
        skills=["Python"],
        highlights=[],
        desired_role="Senior Python Developer",
    )
    _history, item = resolver(tmp_path, profile=profile)

    result = item.resolve(
        Question(0, "Какая роль вам интересна, какие задачи хотели бы выполнять?", "text")
    )

    assert result.status == "resolved"
    assert result.answer == "Senior Python Developer"


def test_questionnaire_answer_schema_and_scope_are_idempotent(tmp_path):
    history = History(tmp_path / "history.db")
    history.set_questionnaire_answer(
        "location", mode="static", payload={"text": "Москва", "choices": []}
    )
    history.set_questionnaire_answer(
        "location", mode="static", payload={"text": "Казань", "choices": []}
    )

    reopened = History(tmp_path / "history.db")

    assert reopened.get_questionnaire_answer("location", "python")["payload"]["text"] == ("Казань")


def test_user_customization_of_seed_template_survives_next_seed_sync(tmp_path):
    history, _item = resolver(tmp_path)
    history.upsert_questionnaire_template(
        "location",
        "Предпочтительная локация",
        "conditions",
        "static",
        "resume",
        source="user",
        confirmed=True,
    )

    QuestionnaireResolver(history, QuestionnaireConfig(enabled=True))

    template = history.get_questionnaire_template("location")
    assert template["label"] == "Предпочтительная локация"
    assert template["default_scope"] == "resume"
    assert template["source"] == "user"

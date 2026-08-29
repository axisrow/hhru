"""Тесты генерации содержимого резюме под кластер вакансий (#753)."""

from __future__ import annotations

import json

import pytest

from hhru_bot.adaptive_resume import (
    _fallback_content,
    _select_experience,
    _shorten_to_one_line,
    build_prompt,
    generate_adaptive_resume,
)
from hhru_bot.config_sections.candidate_facts import (
    CandidateFacts,
    ProjectFact,
    WorkExperienceFact,
)
from hhru_bot.resume_clusters import AI_LLM, DATA_SCIENCE, PYTHON_BACKEND

pytestmark = pytest.mark.unit


class Response:
    def __init__(self, content):
        self.content = content


class Client:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.messages = None

    def chat(self, messages, **kwargs):
        self.messages = messages
        if self.error:
            raise self.error
        return Response(self.content)


def _facts() -> CandidateFacts:
    return CandidateFacts(
        work_experience=[
            WorkExperienceFact(
                company="ООО Данные",
                position="ML-инженер",
                period_from="2022-01",
                period_to="2024-06",
                description="Обучение моделей PyTorch для рекомендательной системы. Metrics.",
                skills=["python", "pytorch", "pandas"],
                tags=["data_science", "ml"],
            ),
            WorkExperienceFact(
                company="ИП Розница",
                position="Продавец-консультант",
                period_from="2018-01",
                period_to="2019-05",
                description="Продажи в магазине электроники. Работа с кассой.",
                skills=[],
                tags=["backend"],  # не пересекается с тегами DATA_SCIENCE
            ),
        ],
        projects=[
            ProjectFact(
                name="Классификатор отзывов",
                description="Пет-проект классификации тональности на sklearn.",
                skills=["python", "sklearn"],
                tags=["data_science", "ml"],
            ),
        ],
    )


# --- _shorten_to_one_line ---


def test_shorten_to_one_line_keeps_first_sentence() -> None:
    assert _shorten_to_one_line("Первое. Второе. Третье.") == "Первое."


def test_shorten_to_one_line_returns_short_text_unchanged() -> None:
    assert _shorten_to_one_line("Короткий текст без точки") == "Короткий текст без точки"


def test_shorten_to_one_line_empty() -> None:
    assert _shorten_to_one_line("") == ""


def test_shorten_to_one_line_does_not_split_on_abbreviation_dot() -> None:
    """Codex cycle-review round 1: точка внутри 'v1.0.' не должна считаться
    границей предложения — реальная граница только после неё."""
    text = "Использовал API v1.0. Оптимизировал отклик."
    assert _shorten_to_one_line(text) == "Использовал API v1.0."


# --- политика отбора: сокращение, а не скрытие ---


def test_irrelevant_experience_is_shortened_not_removed() -> None:
    """Политика #753: нерелевантная запись остаётся (дата не пропадает),
    но описание сокращается до одной строки."""
    facts = _facts()
    selected = _select_experience(facts.work_experience, DATA_SCIENCE)

    assert len(selected) == 2  # ничего не удалено
    relevant, irrelevant = selected
    assert relevant.description == facts.work_experience[0].description  # не тронуто
    assert irrelevant.description == "Продажи в магазине электроники."  # сокращено
    assert irrelevant.company == "ИП Розница"  # компания/даты сохранены
    assert irrelevant.period_from == "2018-01"
    assert irrelevant.period_to == "2019-05"


def test_untagged_fact_is_relevant_to_every_cluster() -> None:
    """#751: пустой tags = релевантен всем кластерам, не отбрасывается никак."""
    fact = WorkExperienceFact(company="X", description="Общий опыт.", tags=[])
    selected = _select_experience([fact], AI_LLM)
    assert selected[0].description == "Общий опыт."  # не сокращено


# --- fallback (без LLM) ---


def test_fallback_never_invents_text() -> None:
    facts = _facts()
    content = _fallback_content(facts, DATA_SCIENCE)

    assert content.source == "fallback"
    assert content.cluster_key == "data_science"
    # Обе записи work_experience присутствуют (сокращение, не удаление).
    assert len(content.work_experience) == 2
    # Навыки кластера идут первыми.
    assert content.skills[0] in ("python", "pytorch", "pandas", "sklearn")


def test_fallback_orders_cluster_skills_first() -> None:
    facts = _facts()
    content = _fallback_content(facts, DATA_SCIENCE)
    keyword_lower = {k.casefold() for k in DATA_SCIENCE.keywords}
    # pytorch/pandas — прямые совпадения с keywords кластера, должны быть
    # раньше python (общий навык, не специфичный для кластера).
    matched_positions = [i for i, s in enumerate(content.skills) if s.casefold() in keyword_lower]
    if matched_positions:
        assert min(matched_positions) <= content.skills.index("python")


# --- build_prompt ---


def test_build_prompt_includes_cluster_and_facts() -> None:
    facts = _facts()
    messages = build_prompt(facts, DATA_SCIENCE)

    assert messages[0]["role"] == "system"
    assert "не выдумывай" in messages[0]["content"].lower()
    user = messages[1]["content"]
    assert "Data Science" in user
    assert "PyTorch" in user or "pytorch" in user.lower()


# --- generate_adaptive_resume: fail-closed contract ---


def test_generate_falls_back_when_llm_client_is_none() -> None:
    content = generate_adaptive_resume(None, _facts(), DATA_SCIENCE)
    assert content.source == "fallback"


def test_generate_falls_back_on_llm_exception() -> None:
    client = Client(error=RuntimeError("timeout"))
    content = generate_adaptive_resume(client, _facts(), DATA_SCIENCE)
    assert content.source == "fallback"
    assert content.work_experience  # не пусто


def test_generate_falls_back_on_malformed_json() -> None:
    client = Client(content="not json at all")
    content = generate_adaptive_resume(client, _facts(), DATA_SCIENCE)
    assert content.source == "fallback"


def test_generate_falls_back_on_wrong_entry_count() -> None:
    """LLM обязан сохранить число записей — иначе неясен маппинг (fail-closed)."""
    payload = json.dumps(
        {
            "title": "ML Engineer",
            "about": "Опытный ML-инженер.",
            "work_experience": ["Только одна запись"],
            "projects": ["Классификатор."],
        }
    )
    client = Client(content=payload)
    content = generate_adaptive_resume(client, _facts(), DATA_SCIENCE)
    assert content.source == "fallback"


def test_generate_uses_llm_output_when_valid() -> None:
    payload = json.dumps(
        {
            "title": "ML-инженер (Data Science)",
            "about": "Специализируюсь на ML-моделях и анализе данных.",
            "work_experience": [
                "Обучение и деплой ML-моделей для рекомендательной системы.",
                "Продажи в магазине электроники.",
            ],
            "projects": ["Классификатор тональности отзывов на sklearn."],
        }
    )
    client = Client(content=payload)
    content = generate_adaptive_resume(client, _facts(), DATA_SCIENCE)

    assert content.source == "llm"
    assert content.title == "ML-инженер (Data Science)"
    assert len(content.work_experience) == 2
    assert content.hidden_note  # одна запись была нерелевантна -> заметка есть


def test_generate_falls_back_when_no_facts_at_all() -> None:
    empty = CandidateFacts()
    client = Client(content="ignored")
    content = generate_adaptive_resume(client, empty, PYTHON_BACKEND)
    assert content.source == "fallback"
    assert client.messages is None  # LLM даже не вызывался

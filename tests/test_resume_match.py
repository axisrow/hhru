"""Keyword matching between an AIProfile and vacancy card text (#492)."""

from __future__ import annotations

import logging

import pytest

from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.config_sections.ai_profile import AIProfile
from hhru_bot.scoring import score_resume_match
from hhru_bot.search import VacancyCard, rank_candidates

pytestmark = pytest.mark.unit


def card(text: str) -> VacancyCard:
    return VacancyCard(
        vacancy_id="1",
        title="Backend developer",
        company="Example",
        url="https://hh.ru/vacancy/1",
        vacancy_text=text,
    )


def test_resume_match_exact_keywords_score_100():
    profile = AIProfile(desired_role="Python developer", skills=["Django", "PostgreSQL"])

    outcome = score_resume_match(
        profile,
        card("Python developer. Stack: Django and PostgreSQL."),
    )

    assert outcome.score_0_100 == pytest.approx(100.0)
    assert sum(outcome.breakdown.values()) == pytest.approx(100.0)


def test_resume_match_partial_keywords():
    profile = AIProfile(skills=["Python", "Django", "PostgreSQL", "Docker"])

    outcome = score_resume_match(profile, card("We use Python and PostgreSQL."))

    assert outcome.score_0_100 == pytest.approx(50.0)


def test_resume_match_no_overlap():
    profile = AIProfile(desired_role="Data analyst", skills=["SQL"])

    assert score_resume_match(profile, card("Java mobile developer")).score_0_100 == 0.0


def test_resume_match_empty_profile():
    assert score_resume_match(AIProfile(), card("Python Django developer")).score_0_100 == 0.0


def test_resume_match_empty_vacancy_text():
    profile = AIProfile(desired_role="Python developer", skills=["Django"])

    assert score_resume_match(profile, card("")).score_0_100 == 0.0


def test_resume_match_topic_does_not_imply_same_intent():
    """#490 regression: a shared salary topic is not a full intent match."""
    profile = AIProfile(highlights=["Рассчитывал зарплату сотрудников"])

    outcome = score_resume_match(profile, card("Укажите ваши зарплатные ожидания"))

    assert 0.0 < outcome.score_0_100 < 50.0


def test_rank_candidates_logs_match_and_keeps_it_out_of_ranking(caplog):
    profile = AIProfile(skills=["Python"])
    resume = ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="developer"),
        ai_profile=profile,
    )
    matching = card("Python developer")
    unrelated = VacancyCard("2", "Developer", "Example", "u", vacancy_text="Java developer")

    with caplog.at_level(logging.INFO, logger="hhru_bot.search"):
        ranked = rank_candidates([unrelated, matching], resume.search, resume)

    # Stage 1 only observes the metric: legacy zero-score order is unchanged.
    assert [item[0].vacancy_id for item in ranked] == ["2", "1"]
    assert [item[2]["resume_match"] for item in ranked] == [0.0, 100.0]
    assert "resume_match score=100.00" in caplog.text
    assert "resume_match score=0.00" in caplog.text

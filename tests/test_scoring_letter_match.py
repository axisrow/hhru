"""Тесты keyword-скоринга сопроводительного письма вакансии (#493)."""

from __future__ import annotations

import pytest

from hhru_bot.scoring import LETTER_MATCH_MODE, letter_match_score
from hhru_bot.scoring.resume_match import NO_DATA_RATIONALE
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


def card(vacancy_text: str = "") -> VacancyCard:
    return VacancyCard(
        vacancy_id="1",
        title="Python-разработчик",
        company="ООО Ромашка",
        url="https://hh.ru/vacancy/1",
        vacancy_text=vacancy_text,
    )


def test_exact_letter_match_scores_high_on_common_scale():
    outcome = letter_match_score(card("Требуется Python, Django, PostgreSQL"), "Python, Django")

    assert outcome.score_0_100 >= 90.0
    assert outcome.mode == LETTER_MATCH_MODE
    assert outcome.breakdown == {"letter": outcome.score_0_100}


def test_partial_letter_match_is_between_full_and_no_match():
    full = letter_match_score(card("Требуется Python, Django"), "Python, Django").score_0_100
    partial = letter_match_score(
        card("Требуется Python, Django"), "Python, Django, Kubernetes"
    ).score_0_100

    assert 0.0 < partial < full


def test_no_overlap_scores_zero():
    outcome = letter_match_score(card("Требуется 1С и бухгалтерский учёт"), "Python и Django")

    assert outcome.score_0_100 == 0.0
    assert outcome.rationale == "совпадений нет"


@pytest.mark.parametrize("letter, vacancy_text", [("", "Python"), ("Python", "")])
def test_empty_letter_or_vacancy_is_no_data(letter: str, vacancy_text: str):
    outcome = letter_match_score(card(vacancy_text), letter)

    assert outcome.score_0_100 == 0.0
    assert outcome.rationale == NO_DATA_RATIONALE
    assert outcome.breakdown == {}


def test_letter_match_reuses_resume_token_matching_rules():
    """Морфология и отрицание остаются едиными с resume-match (#492/#490)."""
    positive = letter_match_score(card("Ищем разработчика по Python"), "разработчик")
    negative = letter_match_score(card("Python не требуется"), "Python")

    assert positive.score_0_100 > 0.0
    assert negative.score_0_100 == 0.0

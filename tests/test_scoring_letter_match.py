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


@pytest.mark.parametrize("letter", ["без Python", "Python не требуется"])
def test_letter_negation_does_not_score_as_full_match(letter: str):
    """Ревью PR #549 (Codex): отрицание В САМОМ ПИСЬМЕ должно снимать
    совпадение, а не давать 100 — кандидат явно заявляет об ОТСУТСТВИИ навыка,
    это не то же самое, что «навык есть»."""
    outcome = letter_match_score(card("Требуется Python"), letter)

    assert outcome.score_0_100 == 0.0
    assert outcome.rationale == "совпадений нет"


def test_letter_negation_is_scoped_to_the_negated_token_not_whole_letter():
    """Отрицание в письме точечное (как resume_match): вычёркивает конкретный
    отрицаемый токен, а не всё письмо. Отличаем от «letter пуст после фильтра»
    (см. соседний тест) сравнением с письмом, где ВСЕ токены под отрицанием.

    Точное числовое значение НЕ проверяем: оно зависит от ширины окна
    ``resume_match._is_negated``, общего кода вне владения этого модуля
    (сузилось после #550) — assert на конкретное число молча привязался бы
    к деталям чужой реализации. Проверяем инвариант, который гарантирует сам
    фикс: отрицаемый токен не засчитан, а несвязанный совпадающий токен —
    засчитан.
    """
    only_negation = letter_match_score(card("Требуется Python"), "без Python").rationale
    mixed = letter_match_score(card("Требуется Python, Kubernetes"), "без Python, знаю Kubernetes")

    # Оба случая дают rationale «совпадений нет» — это честный ноль
    # (letter_text непустой), не NO_DATA_RATIONALE:
    assert only_negation == "совпадений нет"
    assert mixed.rationale == "keyword-match письма"
    # Python отрицаем в письме — не должен внести вклад в 100%; Kubernetes не
    # отрицаем и есть в вакансии — совпадение не должно быть занулено:
    assert 0.0 < mixed.score_0_100 < 100.0


def test_letter_negation_does_not_cross_sentence_boundary():
    """Ревью PR #549 (/review, cycle 3): маркер отрицания в одном предложении
    письма не должен гасить токен навыка из другого предложения. Clause-aware
    ``_is_negated`` (#509) требует передачи ``clause_ids`` — letter-путь обязан
    её использовать, иначе «Я знаю Python. Не требуется SQL» ложно вычёркивает
    python."""
    outcome = letter_match_score(card("Требуется Python, SQL"), "Я знаю Python. Не требуется SQL")

    # python из первого предложения не под отрицанием — совпадение должно
    # засчитаться (score > 0), а не обнулиться из-за «не требуется» во втором:
    assert outcome.score_0_100 > 0.0

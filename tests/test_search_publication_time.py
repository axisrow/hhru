"""Тесты свежести вакансий (issue #429): parse_publication_time + селектор карточки.

Чистая логика без браузера. Регрессионный тест на несовпадение
VACANCY_CARD_PUBLICATION_TIME с реальной разметкой карточки — code review
PR #430 нашёл, что новый селектор не совпадал с тем, что подтверждён
фикстурами (data-qa="vacancy-serp__vacancy-date").
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import hhru_bot.search as search
import hhru_bot.selectors as sel
from hhru_bot.search import _optional_text, parse_publication_time

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
_NOW = datetime(2026, 8, 20, 12, 0, 0)


# --- parse_publication_time --------------------------------------------------


def test_parse_publication_time_today():
    assert parse_publication_time("сегодня", now=_NOW) == _NOW


def test_parse_publication_time_yesterday():
    result = parse_publication_time("вчера", now=_NOW)
    assert result is not None
    assert result.day == _NOW.day - 1


def test_parse_publication_time_singular_day():
    """Регрессия: hh.ru пишет "1 день назад" (не "1 дня/дней назад")."""
    result = parse_publication_time("1 день назад", now=_NOW)
    assert result is not None
    assert (_NOW - result).days == 1


def test_parse_publication_time_plural_few_days():
    result = parse_publication_time("2 дня назад", now=_NOW)
    assert result is not None
    assert (_NOW - result).days == 2


def test_parse_publication_time_plural_many_days():
    result = parse_publication_time("3 дня назад", now=_NOW)
    assert result is not None
    assert (_NOW - result).days == 3


def test_parse_publication_time_none_or_empty_returns_none():
    assert parse_publication_time(None) is None
    assert parse_publication_time("") is None
    assert parse_publication_time("   ") is None


def test_parse_publication_time_unrecognized_returns_none():
    assert parse_publication_time("какой-то мусор") is None


# --- селектор карточки vs реальная разметка ----------------------------------


class _TextLocator:
    def __init__(self, text: str, count: int = 1):
        self._text = text
        self._count = count

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def inner_text(self):
        return self._text


class _FixtureCard:
    """Мок карточки, отдающий текст даты только по подтверждённому data-qa.

    Любой другой селектор — регрессия: если VACANCY_CARD_PUBLICATION_TIME
    разойдётся с разметкой (как в исходном PR #430), тест упадёт с
    AssertionError вместо молчаливого published_at=None.
    """

    CONFIRMED_SELECTOR = "[data-qa='vacancy-serp__vacancy-date']"

    def __init__(self, date_text: str):
        self._date_text = date_text

    def locator(self, selector: str):
        if selector == self.CONFIRMED_SELECTOR:
            return _TextLocator(self._date_text)
        return _TextLocator("", count=0)


def test_publication_time_selector_matches_confirmed_fixture_markup():
    """VACANCY_CARD_PUBLICATION_TIME должен совпадать с фикстурами карточки.

    tests/fixtures/vacancy_card_*.html используют
    data-qa="vacancy-serp__vacancy-date" для даты публикации.
    """
    assert sel.VACANCY_CARD_PUBLICATION_TIME == _FixtureCard.CONFIRMED_SELECTOR


def test_optional_text_reads_publication_time_with_current_selector():
    card = _FixtureCard("вчера")
    text = _optional_text(card, search.sel.VACANCY_CARD_PUBLICATION_TIME)
    assert text == "вчера"


@pytest.mark.parametrize(
    ("fixture_name", "expected_date_text"),
    [
        ("vacancy_card_with_salary.html", "вчера"),
        ("vacancy_card_no_salary.html", "сегодня"),
        ("vacancy_card_with_rating.html", "2 дня назад"),
        ("vacancy_card_salary_usd.html", "3 дня назад"),
    ],
)
def test_fixtures_use_confirmed_publication_date_selector(fixture_name, expected_date_text):
    """Живой контракт: фикстура несёт ожидаемый текст под подтверждённым data-qa."""
    html = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    assert 'data-qa="vacancy-serp__vacancy-date"' in html
    assert expected_date_text in html

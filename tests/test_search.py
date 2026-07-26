"""Characterization-тесты чистой логики search.py.

Страхуют рефакторинг: build_search_url, _extract_vacancy_id, filter_candidates.
Поведение не должно измениться после реструктуризации.
"""

from __future__ import annotations

from hhru_bot.config import SearchFilters
from hhru_bot.search import VacancyCard, _extract_vacancy_id, build_search_url, filter_candidates


class FakeHistory:
    """История, которая знает только заданный набор (resume_id, vacancy_id)."""

    def __init__(self, applied: set[tuple[str, str]] | None = None):
        self._applied = applied or set()

    def has_applied(self, resume_id: str, vacancy_id: str) -> bool:
        return (resume_id, vacancy_id) in self._applied


def card(
    vacancy_id: str, title: str = "T", company: str = "C", url: str = "https://hh.ru/vacancy/0"
):
    return VacancyCard(vacancy_id=vacancy_id, title=title, company=company, url=url)


# --- build_search_url ---


def test_build_search_url_minimal():
    url = build_search_url(SearchFilters(text="python"))
    assert url.startswith("https://hh.ru/search/vacancy?")
    assert "text=python" in url
    assert "page=0" in url


def test_build_search_url_all_filters():
    url = build_search_url(
        SearchFilters(
            text="data analyst",
            area=1,
            salary_from=200000,
            experience="between3And6",
            schedule="remote",
        ),
        page_num=2,
    )
    assert "text=data+analyst" in url
    assert "page=2" in url
    assert "area=1" in url
    assert "salary=200000" in url
    assert "experience=between3And6" in url
    assert "schedule=remote" in url


# --- _extract_vacancy_id ---


def test_extract_vacancy_id_plain_url():
    assert _extract_vacancy_id("https://hh.ru/vacancy/123456") == "123456"


def test_extract_vacancy_id_with_query():
    assert _extract_vacancy_id("/vacancy/98765?from=serp") == "98765"


def test_extract_vacancy_id_non_numeric():
    assert _extract_vacancy_id("https://hh.ru/vacancy/abc") is None


def test_extract_vacancy_id_empty():
    assert _extract_vacancy_id("") is None


# --- filter_candidates ---


def test_filter_candidates_keeps_clean_cards():
    filters = SearchFilters(text="x")
    cards = [card("1"), card("2")]
    candidates, skipped = filter_candidates(cards, filters, "r1", FakeHistory())
    assert candidates == cards
    assert skipped == []


def test_filter_candidates_drops_already_applied():
    filters = SearchFilters(text="x")
    cards = [card("1"), card("2")]
    history = FakeHistory(applied={("r1", "1")})
    candidates, skipped = filter_candidates(cards, filters, "r1", history)
    assert [c.vacancy_id for c in candidates] == ["2"]
    assert len(skipped) == 1
    assert skipped[0][0].vacancy_id == "1"
    assert "уже откликались" in skipped[0][1]


def test_filter_candidates_excludes_employers():
    filters = SearchFilters(text="x", exclude_employers=["BadCorp"])
    cards = [
        card("1", title="Dev", company="GoodCorp"),
        card("2", title="Dev", company="BadCorp Inc"),
    ]
    candidates, skipped = filter_candidates(cards, filters, "r1", FakeHistory())
    assert [c.vacancy_id for c in candidates] == ["1"]
    assert skipped[0][0].vacancy_id == "2"
    assert "стоп-списке" in skipped[0][1]


def test_filter_candidates_excludes_keywords():
    filters = SearchFilters(text="x", exclude_keywords=["1С"])
    cards = [
        card("1", title="Python Dev"),
        card("2", title="Программист 1С"),
    ]
    candidates, skipped = filter_candidates(cards, filters, "r1", FakeHistory())
    assert [c.vacancy_id for c in candidates] == ["1"]
    assert skipped[0][0].vacancy_id == "2"
    assert "стоп-слово" in skipped[0][1]

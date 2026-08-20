"""Cross-resume vacancy routing tests (issue #418)."""

import pytest

from hhru_bot.apply.router import MergedVacancy, merge_vacancies, route_vacancies
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.history import History
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


def resume(name: str, text: str = "python") -> ResumeConfig:
    return ResumeConfig(name, f"https://hh.ru/resume/{name}", SearchFilters(text=text))


def card(vacancy_id: str, title: str) -> VacancyCard:
    return VacancyCard(vacancy_id, title, "Acme", f"https://hh.ru/vacancy/{vacancy_id}")


def test_merge_vacancies_deduplicates_and_keeps_source_hints():
    r1, r2 = resume("one"), resume("two")
    merged = merge_vacancies(
        [(r1, [card("v1", "Python")]), (r2, [card("v1", "Python"), card("v2", "Go")])]
    )
    assert [(v.card.vacancy_id, v.source_resume_ids) for v in merged] == [
        ("v1", ("one", "two")),
        ("v2", ("two",)),
    ]


def test_route_chooses_best_matching_resume_and_reason(tmp_path):
    r1, r2 = resume("python", "python"), resume("go", "go")
    items = [MergedVacancy(card("v1", "Go developer"), (r1.resume_id, r2.resume_id))]
    selected = route_vacancies(items, [r1, r2], History(tmp_path / "history.db"))
    assert selected["v1"].resume is r2
    assert "selected resume go" in selected["v1"].reason


def test_route_does_not_cross_a_positive_search_boundary(tmp_path):
    python = resume("python", "python")
    go = resume("go", "go")
    item = MergedVacancy(card("v1", "Go developer"), (python.resume_id,))
    selected = route_vacancies([item], [python, go], History(tmp_path / "history.db"))
    assert selected["v1"].resume is python


def test_route_fails_closed_for_invalid_identity(tmp_path):
    invalid = ResumeConfig("bad", "https://hh.ru/resume/XXXXXXXX", SearchFilters(text="python"))
    selected = route_vacancies(
        [MergedVacancy(card("v1", "Python"), ())], [invalid], History(tmp_path / "history.db")
    )
    assert selected == {}

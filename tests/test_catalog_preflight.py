"""Чистая сверка листов и read-only pre-flight --area (#950)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hhru_bot.catalog_preflight as module
from hhru_bot.catalog_preflight import (
    PreflightOutcome,
    evaluate_leaf,
    format_candidates,
    preflight_area,
)
from hhru_bot.professional_roles import ProfessionalRole

pytestmark = pytest.mark.unit


def test_evaluate_leaf_accepts_normalized_exact_match():
    evaluation = evaluate_leaf("  Врач ", ["Ассистент врача", "Врач", "Другое"])

    assert evaluation.exact is True


def test_evaluate_leaf_missing_leaf_collects_substring_candidates():
    evaluation = evaluate_leaf("врач", ["Ассистент врача", "Ветеринарный врач", "Бухгалтер"])

    assert evaluation.exact is False
    assert evaluation.candidates == ("Ассистент врача", "Ветеринарный врач")


def test_evaluate_leaf_matches_leaf_contained_in_query():
    """Боевой кейс #950: запрос «Врач-хирург», а в дереве только общий «Врач»."""
    evaluation = evaluate_leaf("Врач-хирург", ["Врач", "Ветеринарный врач"])

    assert evaluation.exact is False
    # «Ветеринарный врач» не совпадает с запросом ни по одной подстроке.
    assert evaluation.candidates == ("Врач",)


def test_evaluate_leaf_deduplicates_and_keeps_first_order():
    evaluation = evaluate_leaf(
        "менеджер",
        [
            "Менеджер по продажам, менеджер по работе с клиентами",
            "Менеджер по продажам, менеджер по работе с клиентами",
            "Менеджер по закупкам",
        ],
    )

    assert evaluation.candidates == (
        "Менеджер по продажам, менеджер по работе с клиентами",
        "Менеджер по закупкам",
    )


def test_evaluate_leaf_never_offers_placeholder_as_candidate():
    """«Другое» — вырожденный catch-all: перезапуск по нему невозможен (#913)."""
    evaluation = evaluate_leaf("хирург", ["Другое", "Врач"])

    assert evaluation.exact is False
    assert "Другое" not in evaluation.candidates
    assert evaluation.candidates == ()


def test_evaluate_leaf_exact_placeholder_is_still_exact():
    """Явный запрос «Другое» — точный лист: авто-отказ на нём был бы ложным."""
    assert evaluate_leaf("другое", ["Другое"]).exact is True


def test_format_candidates_lists_or_reports_empty():
    listed = format_candidates(evaluate_leaf("врач", ["Врач", "Ассистент врача"]))
    empty = format_candidates(evaluate_leaf("хирург", ["Другое"]))

    assert listed == "ближайшие доступные листы: Врач; Ассистент врача"
    assert empty == "совпадений по подстроке не найдено"


def test_preflight_area_exact_leaf_passes_silently(monkeypatch):
    seen: dict[str, object] = {}

    def fake_search(page, queries):
        seen["queries"] = queries
        return [ProfessionalRole("148", "Врач", "Медицина")]

    monkeypatch.setattr(module, "search_professional_roles", fake_search)

    outcome = preflight_area(SimpleNamespace(), "Врач")

    assert outcome == PreflightOutcome(True, "")
    assert seen["queries"] == ["Врач"]


def test_preflight_area_missing_leaf_refuses_with_candidates(monkeypatch):
    monkeypatch.setattr(
        module,
        "search_professional_roles",
        lambda page, queries: [
            ProfessionalRole("148", "Врач", "Медицина"),
            ProfessionalRole("149", "Ассистент врача", "Медицина"),
            ProfessionalRole("40", "Другое", "Медицина"),
        ],
    )

    outcome = preflight_area(SimpleNamespace(), "Врач-хирург")

    assert outcome.ok is False
    assert "не найдена в live-каталоге" in outcome.message
    # «Ассистент врача» не содержится в запросе ни в одну сторону — в перечень
    # попадает только лист «Врач», содержащийся в запросе целиком (#950).
    assert "ближайшие доступные листы: Врач" in outcome.message
    assert "Ассистент врача" not in outcome.message
    assert "Другое" not in outcome.message
    assert "--allow-unresolved-area" in outcome.message


def test_preflight_area_placeholder_only_filter_retries_then_refuses(monkeypatch):
    """Нестабильность фильтра #920: вырожденный ответ переспрашивается один раз."""
    calls: list[list[str]] = []

    def fake_search(page, queries):
        calls.append(list(queries))
        return [ProfessionalRole("40", "Другое", "Медицина")]

    monkeypatch.setattr(module, "search_professional_roles", fake_search)

    outcome = preflight_area(SimpleNamespace(), "Врач-хирург")

    assert calls == [["Врач-хирург"], ["Врач-хирург"]]
    assert outcome.ok is False
    assert "совпадений по подстроке не найдено" in outcome.message


def test_preflight_area_second_attempt_finds_leaf(monkeypatch):
    responses: list[list[ProfessionalRole]] = [
        [ProfessionalRole("40", "Другое", "Медицина")],
        [ProfessionalRole("148", "Врач", "Медицина")],
    ]

    def fake_search(page, queries):
        return responses.pop(0)

    monkeypatch.setattr(module, "search_professional_roles", fake_search)

    assert preflight_area(SimpleNamespace(), "Врач").ok is True


def test_preflight_area_allow_unresolved_passes_with_warning(monkeypatch):
    monkeypatch.setattr(
        module,
        "search_professional_roles",
        lambda page, queries: [ProfessionalRole("148", "Врач", "Медицина")],
    )

    outcome = preflight_area(SimpleNamespace(), "Врач-хирург", allow_unresolved_area=True)

    assert outcome.ok is True
    assert "[WARN]" not in outcome.message
    assert "«Другое» (id 40)" in outcome.message

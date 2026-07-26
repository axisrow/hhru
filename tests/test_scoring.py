"""Тесты скоринга/ранжирования вакансий (issue #15).

Чистая логика без браузера. rank_candidates сортирует кандидатов по убыванию
score, где score = взвешенная сумма факторов (буст за must_have/nice_to_have и
совпадение стека, штраф за близость к стоп-словам). Поведение должно быть
детерминированным и обратно совместимым при пустых весах/списках.
"""

from __future__ import annotations

import textwrap

import pytest

from hhru_bot.config import ResumeConfig, SearchFilters, load_config
from hhru_bot.config_sections.scoring import ScoringConfig, ScoringWeights
from hhru_bot.search import VacancyCard, rank_candidates


def card(vacancy_id: str, title: str = "T", company: str = "C"):
    return VacancyCard(
        vacancy_id=vacancy_id, title=title, company=company, url="https://hh.ru/vacancy/0"
    )


def resume(
    search: SearchFilters | None = None,
    scoring=None,
) -> ResumeConfig:
    """resume с явно заданным scoring (по умолчанию None — legacy-путь, нулевые веса)."""
    return ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/AAA111",
        search=search or SearchFilters(text="python"),
        scoring=scoring,
    )


def resume_scored(search: SearchFilters, weights: ScoringWeights | None = None) -> ResumeConfig:
    """resume с включённым scoring и дефолтными весами факторов.

    Для тестов самих факторов (must_have/nice_to_have/exclude/text_match) —
    в отличие от legacy-тестов, где scoring=None и веса истинно нулевые.
    """
    return resume(search=search, scoring=ScoringConfig(weights=weights or ScoringWeights()))


# --- обратная совместимость: нет scoring, нет must_have/nice_to_have ---


def test_rank_empty_weights_preserves_order():
    """Без scoring-конфига все score = 0.0, порядок входа сохраняется."""
    filters = SearchFilters(text="python")
    cards = [card("1", title="A"), card("2", title="B"), card("3", title="C")]
    ranked = rank_candidates(cards, filters, resume(search=filters))
    assert [c.vacancy_id for c, _s, _b in ranked] == ["1", "2", "3"]
    assert all(score == 0.0 for _c, score, _b in ranked)


def test_legacy_scoring_zero_scores_even_when_text_matches():
    """Без scoring-секции score обязан быть 0 даже если filters.text матчит title.

    Регрессия (codex critical): text_match=1.0 в дефолтных весах делал legacy
    score ненулевым, нарушая обратную совместимость. Дефолт без scoring должен
    быть истинно нейтральным — все факторы 0.
    """
    filters = SearchFilters(text="python developer")
    cards = [
        card("1", title="Python Developer"),
        card("2", title="Senior Python Developer"),
    ]
    ranked = rank_candidates(cards, filters, resume(search=filters))  # scoring=None
    assert all(score == 0.0 for _c, score, _b in ranked)


def test_legacy_scoring_preserves_input_order_with_matching_text_and_mixed_ids():
    """Без scoring-секции порядок входа сохраняется, даже когда:

    - title матчит filters.text (равные score),
    - vacancy_id идут не по возрастанию (т.е. лексический тай-брейк по id
      перевернул бы порядок).

    Гарантирует, что ranked[:limit] при legacy-конфиге выбирает ТЕ ЖЕ первые N
    вакансий, что и старый candidates[:limit] — дневной лимит не уходит на
    другой набор. (codex critical: раньше ids 300,200,100 → 100,200,300.)
    """
    filters = SearchFilters(text="python developer")
    cards = [
        card("300", title="Python Developer"),
        card("200", title="Senior Python Developer"),
        card("100", title="Python Developer Remote"),
    ]
    ranked = rank_candidates(cards, filters, resume(search=filters))
    assert [c.vacancy_id for c, _s, _b in ranked] == ["300", "200", "100"]
    # [:limit] должен давать тот же набор, что и входной срез
    assert [c.vacancy_id for c, _s, _b in ranked[:1]] == ["300"]


def test_rank_empty_text_zero_text_match():
    """filters.text='' → text_ratio=0, фактор text_match отсутствует (ветка)."""
    filters = SearchFilters(text="")
    cards = [card("1", title="Python")]
    ranked = rank_candidates(cards, filters, resume(search=filters))
    _c, score, breakdown = ranked[0]
    assert breakdown["text_match"] == 0.0
    assert score == 0.0


def test_rank_empty_input():
    assert rank_candidates([], SearchFilters(text="x"), resume()) == []


# --- фактор must_have ---


def test_factor_must_have_boosts_matching_title():
    filters = SearchFilters(text="python", must_have=["django"])
    cards = [
        card("1", title="Python Developer"),  # без django
        card("2", title="Python Django Developer"),  # django в title
    ]
    ranked = rank_candidates(cards, filters, resume_scored(filters))
    by_id = {c.vacancy_id: (s, b) for c, s, b in ranked}
    # must_have-матч должен дать строго больший score
    assert by_id["2"][0] > by_id["1"][0]
    assert by_id["2"][1]["must_have"] > 0.0
    assert by_id["1"][1]["must_have"] == 0.0


def test_factor_must_have_counts_multiple_keywords():
    filters = SearchFilters(text="python", must_have=["django", "flask"])
    cards = [card("1", title="Python Django Flask Developer")]
    ranked = rank_candidates(cards, filters, resume_scored(filters))
    _c, score, breakdown = ranked[0]
    # оба must_have найдены — буст должен быть больше, чем за один
    assert breakdown["must_have"] > 0.0
    assert score == pytest.approx(sum(breakdown.values()))


# --- фактор nice_to_have ---


def test_factor_nice_to_have_lesser_than_must_have():
    filters = SearchFilters(text="python", must_have=["django"], nice_to_have=["docker"])
    cards = [
        card("a", title="Python Django Developer"),  # только must_have
        card("b", title="Python Docker Developer"),  # только nice_to_have
        card("c", title="Python Developer"),  # ничего
    ]
    ranked = rank_candidates(cards, filters, resume_scored(filters))
    order = [c.vacancy_id for c, _s, _b in ranked]
    # must_have > nice_to_have > ничего
    assert order == ["a", "b", "c"]


# --- штраф за близость к стоп-словам ---


def test_factor_exclude_keyword_penalty():
    filters = SearchFilters(text="python", exclude_keywords=["1С"])
    cards = [
        card("1", title="Python Developer"),
        card("2", title="Программист 1С"),
    ]
    ranked = rank_candidates(cards, filters, resume_scored(filters))
    by_id = {c.vacancy_id: (s, b) for c, s, b in ranked}
    # стоп-слово в title должно штрафовать (отрицательный фактор)
    assert by_id["2"][1]["exclude_keyword"] < 0.0
    assert by_id["1"][0] > by_id["2"][0]


# --- совпадение стека (text → title) ---


def test_factor_text_match():
    filters = SearchFilters(text="python developer")
    cards = [
        card("1", title="Python Developer"),  # оба токена
        card("2", title="Java Developer"),  # один токен
        card("3", title="Project Manager"),  # ни одного
    ]
    ranked = rank_candidates(cards, filters, resume_scored(filters))
    by_id = {c.vacancy_id: s for c, s, _b in ranked}
    assert by_id["1"] > by_id["2"] > by_id["3"]


# --- детерминизм ---


def test_tiebreak_stable_by_input_order_when_equal_score():
    """Равные score → стабильный ВХОДНОЙ порядок (Timsort), а не лексически по id.

    Тай-брейк по vacancy_id ломал бы обратную совместимость [:limit] при
    перемешанных id (codex critical). Сортировка только по score.
    """
    filters = SearchFilters(text="x")  # ничего не матчит → score 0 у всех
    cards = [
        card("3", title="Same"),
        card("1", title="Same"),
        card("2", title="Same"),
    ]
    ranked = rank_candidates(cards, filters, resume(search=filters))
    assert [c.vacancy_id for c, _s, _b in ranked] == ["3", "1", "2"]


def test_rank_deterministic_repeated_calls():
    filters = SearchFilters(text="python", must_have=["django"], nice_to_have=["docker"])
    cards = [
        card("3", title="Python Developer"),
        card("1", title="Python Django Docker"),
        card("2", title="Python Django"),
    ]
    r1 = [
        (c.vacancy_id, round(s, 6))
        for c, s, _b in rank_candidates(cards, filters, resume_scored(filters))
    ]
    r2 = [
        (c.vacancy_id, round(s, 6))
        for c, s, _b in rank_candidates(cards, filters, resume_scored(filters))
    ]
    assert r1 == r2


def test_does_not_mutate_input():
    filters = SearchFilters(text="python", must_have=["django"])
    cards = [card("2", title="B"), card("1", title="A")]
    original = list(cards)
    rank_candidates(cards, filters, resume(search=filters))
    assert cards == original


# --- веса из config ---


def test_scoring_weights_applied():
    from hhru_bot.config_sections.scoring import ScoringConfig, ScoringWeights

    weights = ScoringWeights(must_have=10.0, nice_to_have=1.0, exclude_keyword=0.0, text_match=0.0)
    filters = SearchFilters(text="python", must_have=["django"])
    cards = [card("1", title="Python Django")]
    ranked = rank_candidates(
        cards, filters, resume(search=filters, scoring=ScoringConfig(weights=weights))
    )
    _c, score, breakdown = ranked[0]
    assert breakdown["must_have"] == pytest.approx(10.0)
    assert score == pytest.approx(10.0)


def test_load_config_scoring_section(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            account:
              storage_state_file: data/storage_state/hh_session.json
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/AAA111"
                search:
                  text: "python"
                  must_have: ["django"]
                  nice_to_have: ["docker"]
                scoring:
                  weights:
                    must_have: 5.0
                    nice_to_have: 2.0
                    exclude_keyword: -4.0
                    text_match: 1.0
            """
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    r: ResumeConfig = config.resumes[0]
    assert r.scoring is not None
    assert r.scoring.weights.must_have == 5.0
    assert r.scoring.weights.nice_to_have == 2.0
    assert r.scoring.weights.exclude_keyword == -4.0
    assert r.scoring.weights.text_match == 1.0
    assert r.search.must_have == ["django"]
    assert r.search.nice_to_have == ["docker"]


def test_load_config_scoring_optional_defaults(tmp_path):
    """Без секции scoring resume.scoring = None; SearchFilters.must_have/nice_to_have = []."""
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            account:
              storage_state_file: data/storage_state/hh_session.json
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/AAA111"
                search:
                  text: "python"
            """
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    r: ResumeConfig = config.resumes[0]
    assert r.scoring is None
    assert r.search.must_have == []
    assert r.search.nice_to_have == []

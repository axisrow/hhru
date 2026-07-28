"""Тесты эвристического pre-LLM фильтра работодателя (issue #85).

Чистая логика без браузера/LLM. Фильтр отсекает «слепые отклики в пустоту»
ДО LLM-скоринга (#74) — 0 токенов. Покрывает:
  - employer_passes_prefilter: disabled→pass (обратная совместимость),
    top_tech/big_corp→pass, trusted→pass, rating/reviews≥порога→pass,
    employer_interacted→pass, unknown+ничего→skip.
  - history.employer_interacted: account-scope JOIN responses/manual_offers по
    vacancy_id и employer.
  - parse_scoring: prefilter отсутствует→None (откл.), enabled+пороги, не-число.
  - filter_candidates: pre-фильтр применяется после дедупа/стоп-листов.
"""

from __future__ import annotations

import textwrap

import pytest

from hhru_bot.config import ConfigError, SearchFilters, load_config
from hhru_bot.config_sections.scoring import PrefilterConfig, parse_scoring
from hhru_bot.history import History
from hhru_bot.scoring import (
    PREFILTER_SKIP_REASON,
    EmployerInfo,
    employer_passes_prefilter,
)
from hhru_bot.search import VacancyCard, filter_candidates

# --- хелперы ----------------------------------------------------------------


def card(
    vacancy_id: str = "1",
    title: str = "Python Developer",
    company: str = "ООО Ромашка",
    employer_info: EmployerInfo | None = None,
) -> VacancyCard:
    return VacancyCard(
        vacancy_id=vacancy_id,
        title=title,
        company=company,
        url="https://hh.ru/vacancy/0",
        employer_info=employer_info,
    )


class FakeHistory:
    """История с заглушками has_applied и employer_interacted для чистых тестов."""

    def __init__(self, applied: set | None = None, interacted: bool = False):
        self._applied = applied or set()
        self._interacted = interacted

    def has_applied(self, resume_id: str, vacancy_id: str) -> bool:
        return (resume_id, vacancy_id) in self._applied

    def employer_interacted(self, vacancy_id=None, employer=None, resume_id=None) -> bool:
        return self._interacted


THRESHOLDS = PrefilterConfig(enabled=True, rating_min=3.5, reviews_min=10)


# --- employer_passes_prefilter: обратная совместимость ----------------------


def test_prefilter_disabled_when_thresholds_none():
    """thresholds=None → фильтр откл., проходит ВСЁ (обратная совместимость)."""
    passes, reason = employer_passes_prefilter(card(), FakeHistory(), "r1", None)
    assert passes is True
    assert reason == ""


def test_prefilter_disabled_when_not_enabled():
    """enabled=False (дефолт) → проходит, даже неизвестная компания."""
    disabled = PrefilterConfig(enabled=False)
    passes, reason = employer_passes_prefilter(card(), FakeHistory(), "r1", disabled)
    assert passes is True
    assert reason == ""


def test_prefilter_passes_when_history_none():
    """history=None → нет данных о взаимодействии, безопасно пропускаем."""
    passes, reason = employer_passes_prefilter(card(), None, "r1", THRESHOLDS)
    assert passes is True
    assert reason == ""


# --- employer_passes_prefilter: позитивные сигналы --------------------------


def test_prefilter_top_tech_passes():
    """Известная компания (Яндекс = top_tech) проходит без rating/reviews."""
    c = card(company="Яндекс", employer_info=None)
    passes, reason = employer_passes_prefilter(c, FakeHistory(), "r1", THRESHOLDS)
    assert passes is True
    assert reason == ""


def test_prefilter_big_corp_passes():
    """big_corp (МТС) проходит без rating/reviews."""
    c = card(company="МТС", employer_info=None)
    passes, reason = employer_passes_prefilter(c, FakeHistory(), "r1", THRESHOLDS)
    assert passes is True
    assert reason == ""


def test_prefilter_trusted_passes():
    """trusted-бейдж hh.ru проходит даже без rating."""
    c = card(company="Новая Компания", employer_info=EmployerInfo(trusted=True))
    passes, reason = employer_passes_prefilter(c, FakeHistory(), "r1", THRESHOLDS)
    assert passes is True
    assert reason == ""


def test_prefilter_rating_at_threshold_passes():
    """rating >= rating_min проходит (граница включается)."""
    c = card(employer_info=EmployerInfo(rating=3.5))
    passes, reason = employer_passes_prefilter(c, FakeHistory(), "r1", THRESHOLDS)
    assert passes is True
    assert reason == ""


def test_prefilter_reviews_at_threshold_passes():
    """reviews_count >= reviews_min проходит (граница включается)."""
    c = card(employer_info=EmployerInfo(reviews_count=10))
    passes, reason = employer_passes_prefilter(c, FakeHistory(), "r1", THRESHOLDS)
    assert passes is True
    assert reason == ""


def test_prefilter_interaction_passes():
    """Работодатель раньше приглашал/смотрел → проходит даже unknown."""
    c = card(company="ООО Ромашка", employer_info=None)
    passes, reason = employer_passes_prefilter(c, FakeHistory(interacted=True), "r1", THRESHOLDS)
    assert passes is True
    assert reason == ""


# --- employer_passes_prefilter: отсев ---------------------------------------


def test_prefilter_unknown_no_signal_skips():
    """Неизвестная компания без rating/reviews/взаимодействия → отсев."""
    c = card(company="ООО Ромашка", employer_info=None)
    passes, reason = employer_passes_prefilter(c, FakeHistory(interacted=False), "r1", THRESHOLDS)
    assert passes is False
    assert reason == PREFILTER_SKIP_REASON


def test_prefilter_low_rating_no_reviews_skips():
    """rating ниже порога И reviews ниже порога → отсев (нет иного сигнала)."""
    c = card(employer_info=EmployerInfo(rating=2.0, reviews_count=3))
    passes, reason = employer_passes_prefilter(c, FakeHistory(interacted=False), "r1", THRESHOLDS)
    assert passes is False


def test_prefilter_employer_info_none_unknown_company_skips():
    """employer_info=None + неизвестное имя → отсев (нет блоков hh.ru)."""
    c = card(company="Совсем Неизвестно", employer_info=None)
    passes, _ = employer_passes_prefilter(c, FakeHistory(), "r1", THRESHOLDS)
    assert passes is False


# --- history.employer_interacted (SQLite) -----------------------------------


def test_employer_interacted_no_args_false(tmp_path):
    h = History(tmp_path / "h.db")
    assert h.employer_interacted() is False


def test_employer_interacted_by_vacancy_id_true(tmp_path):
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "invitation", "/chat/1")
    assert h.employer_interacted(vacancy_id="v1") is True


def test_employer_interacted_by_employer_name_true(tmp_path):
    """Матч по имени компании: работодатель отвечал по ДРУГОЙ вакансии."""
    h = History(tmp_path / "h.db")
    h.upsert_response("v_old", "Acme", "invitation", "/chat/1")
    assert h.employer_interacted(employer="Acme") is True


def test_employer_interacted_read_status_counts(tmp_path):
    """read = работодатель посмотрел резюме — валидный сигнал интереса."""
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "read", "/chat/1")
    assert h.employer_interacted(vacancy_id="v1") is True


def test_employer_interacted_no_match_false(tmp_path):
    """Нет responses по вакансии/работодателю → False."""
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "invitation", "/chat/1")
    assert h.employer_interacted(vacancy_id="v999") is False
    assert h.employer_interacted(employer="Nobody") is False


def test_employer_interacted_manual_offer_counts(tmp_path):
    """Липкая ручная пометка оффера (#13) — тоже сигнал взаимодействия."""
    h = History(tmp_path / "h.db")
    h.mark_offer("v1", "r1")
    assert h.employer_interacted(vacancy_id="v1") is True


def test_employer_interacted_both_vacancy_and_employer(tmp_path):
    """Комбинированный запрос: оба критерия в одном вызове."""
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "discard", "/chat/1")
    # discard = отказ, но резюме видели → взаимодействие есть.
    assert h.employer_interacted(vacancy_id="v1", employer="Acme") is True


def test_employer_interacted_vacancy_id_none_employer_match(tmp_path):
    """vacancy_id=None + employer — матч по имени компании (account-scope).

    Сценарий pre-фильтра: новой карточки ещё нет в responses, но работодатель
    отвечал по ДРУГОЙ своей вакансии. vacancy_id=None (новая) → поиск только по
    employer; SQLite `vacancy_id = ?` с None не добавляется в clauses.
    """
    h = History(tmp_path / "h.db")
    h.upsert_response("v_old", "Acme", "invitation", "/chat/1")
    assert h.employer_interacted(vacancy_id=None, employer="Acme") is True
    assert h.employer_interacted(vacancy_id=None, employer="Nobody") is False


# --- parse_scoring: prefilter ----------------------------------------------


def test_parse_scoring_no_prefilter_is_none():
    """Без подсекции prefilter → None (фильтр откл.)."""
    cfg = parse_scoring({"weights": {"must_have": 3.0}}, "scoring")
    assert cfg is not None
    assert cfg.prefilter is None


def test_parse_scoring_prefilter_disabled_by_default():
    """Пустой prefilter → None; enabled не задан → дефолт disabled."""
    cfg = parse_scoring({"prefilter": {}}, "scoring")
    assert cfg.prefilter is None  # пустой raw → None (как weights)


def test_parse_scoring_prefilter_enabled_with_thresholds():
    cfg = parse_scoring(
        {"prefilter": {"enabled": True, "rating_min": 4.0, "reviews_min": 50}},
        "scoring",
    )
    assert cfg.prefilter is not None
    assert cfg.prefilter.enabled is True
    assert cfg.prefilter.rating_min == 4.0
    assert cfg.prefilter.reviews_min == 50


def test_parse_scoring_prefilter_enabled_uses_soft_defaults():
    """enabled без порогов → мягкие дефолты PrefilterConfig."""
    cfg = parse_scoring({"prefilter": {"enabled": True}}, "scoring")
    assert cfg.prefilter is not None
    assert cfg.prefilter.enabled is True
    assert cfg.prefilter.rating_min == PrefilterConfig.rating_min
    assert cfg.prefilter.reviews_min == PrefilterConfig.reviews_min


def test_parse_scoring_prefilter_bad_rating_raises():
    with pytest.raises(ConfigError):
        parse_scoring({"prefilter": {"enabled": True, "rating_min": "oops"}}, "scoring")


# --- filter_candidates: интеграция pre-фильтра ------------------------------


def test_filter_candidates_prefilter_skips_low_employer_signal():
    """Pre-фильтр enabled: unknown-карточка без сигнала → [skip]."""
    filters = SearchFilters(text="x")
    cards = [
        card("1", company="Яндекс"),  # top_tech → проходит
        card("2", company="ООО Ромашка"),  # unknown → отсев
    ]
    candidates, skipped = filter_candidates(
        cards, filters, "r1", FakeHistory(interacted=False), THRESHOLDS
    )
    assert [c.vacancy_id for c in candidates] == ["1"]
    assert len(skipped) == 1
    assert skipped[0][0].vacancy_id == "2"
    assert skipped[0][1] == PREFILTER_SKIP_REASON


def test_filter_candidates_prefilter_disabled_keeps_all():
    """thresholds=None → pre-фильтр откл., всё проходит (обратная совместимость)."""
    filters = SearchFilters(text="x")
    cards = [card("1", company="ООО Ромашка")]
    candidates, skipped = filter_candidates(cards, filters, "r1", FakeHistory(), None)
    assert len(candidates) == 1
    assert skipped == []


def test_filter_candidates_prefilter_after_dedup_and_stoplist():
    """Pre-фильтр идёт ПОСЛЕ дедупа/стоп-листов (более определённые причины)."""
    filters = SearchFilters(text="x", exclude_employers=["BadCorp"])
    cards = [
        card("1", company="ООО Ромашка"),  # unknown, но не в стопе → pre-фильтр skip
        card("2", company="BadCorp"),  # стоп-список → причина «стоп-список», не pre-фильтр
    ]
    history = FakeHistory(applied={("r1", "3")})
    cards.append(card("3", company="ООО Ромашка"))  # уже откликались → dedup
    candidates, skipped = filter_candidates(cards, filters, "r1", history, THRESHOLDS)
    assert candidates == []
    reasons = {c.vacancy_id: r for c, r in skipped}
    assert "уже откликались" in reasons["3"]
    assert "стоп-списке" in reasons["2"]
    assert reasons["1"] == PREFILTER_SKIP_REASON


# --- полная интеграция: load_config → resume.scoring.prefilter --------------


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_config_prefilter_section(tmp_path):
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python"
            scoring:
              weights:
                must_have: 3.0
              prefilter:
                enabled: true
                rating_min: 4.0
                reviews_min: 25
        """,
    )
    config = load_config(path)
    resume = config.resumes[0]
    assert resume.scoring is not None
    assert resume.scoring.prefilter is not None
    assert resume.scoring.prefilter.enabled is True
    assert resume.scoring.prefilter.rating_min == 4.0
    assert resume.scoring.prefilter.reviews_min == 25


def test_load_config_prefilter_absent_is_none(tmp_path):
    """Без секции scoring → resume.scoring=None → prefilter нет (обратная совместимость)."""
    path = _write_config(
        tmp_path,
        """
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python"
        """,
    )
    config = load_config(path)
    assert config.resumes[0].scoring is None

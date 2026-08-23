"""Тесты keyword-скоринга соответствия резюме вакансии (issue #492, Этап 1).

Чистая функция без браузера и без LLM: ``resume_match_score`` сопоставляет
``AIProfile`` (summary/skills/highlights/desired_role) с
``VacancyCard.vacancy_text`` взвешенным пересечением токенов и возвращает
``ScoreOutcome`` на ОБЩЕЙ шкале 0-100 (та же, что у эвристики/LLM — см. #74 F2).

Покрываются кейсы из issue: точное совпадение навыков, частичное, полное
несовпадение, пустой профиль, пустой текст вакансии, плюс regression на класс
ошибки #490 («keyword ловит тему, но не намерение»): требование ОТСУТСТВИЯ
навыка не должно засчитываться как совпадение.
"""

from __future__ import annotations

import pytest

from hhru_bot.config_sections.ai_profile import AIProfile
from hhru_bot.scoring import RESUME_MATCH_MODE, resume_match_score
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit

# --- хелперы ----------------------------------------------------------------


def card(vacancy_text: str = "", title: str = "Python-разработчик") -> VacancyCard:
    return VacancyCard(
        vacancy_id="1",
        title=title,
        company="ООО Ромашка",
        url="https://hh.ru/vacancy/1",
        vacancy_text=vacancy_text,
    )


def profile(**kwargs) -> AIProfile:
    return AIProfile(**kwargs)


# --- шкала и контракт результата --------------------------------------------


def test_returns_score_outcome_on_0_100_scale():
    """Шкала переиспользована из ScoreOutcome (#492: НЕ заводить 0-1)."""
    outcome = resume_match_score(
        card("Требуется Python, Django, PostgreSQL"),
        profile(skills=["Python", "Django", "PostgreSQL"]),
    )
    assert 0.0 <= outcome.score_0_100 <= 100.0
    assert outcome.mode == RESUME_MATCH_MODE


def test_breakdown_exposes_factors_for_logging():
    """Этап 1 — только наблюдение: breakdown должен объяснять score в логах."""
    outcome = resume_match_score(
        card("Требуется Python и Django"),
        profile(skills=["Python", "Django"], desired_role="Python-разработчик"),
    )
    assert outcome.breakdown
    assert set(outcome.breakdown) <= {"skills", "desired_role", "summary", "highlights"}


# --- совпадения --------------------------------------------------------------


def test_exact_skill_match_scores_high():
    outcome = resume_match_score(
        card("Ищем разработчика: Python, Django, PostgreSQL, Docker"),
        profile(skills=["Python", "Django", "PostgreSQL", "Docker"]),
    )
    assert outcome.score_0_100 >= 90.0


def test_partial_match_scores_between_extremes():
    """Половина навыков найдена — score строго между полным промахом и полным матчем."""
    full = resume_match_score(
        card("Требуется Python, Django"),
        profile(skills=["Python", "Django"]),
    ).score_0_100
    partial = resume_match_score(
        card("Требуется Python, Django"),
        profile(skills=["Python", "Django", "Kubernetes", "Terraform"]),
    ).score_0_100
    assert 0.0 < partial < full


def test_no_overlap_scores_zero():
    outcome = resume_match_score(
        card("Требуется 1С, бухгалтерский учёт, УТ 11"),
        profile(skills=["Python", "Django", "PostgreSQL"]),
    )
    assert outcome.score_0_100 == 0.0


def test_desired_role_matches_vacancy_title_text():
    """desired_role — отдельный фактор, а не часть skills."""
    outcome = resume_match_score(
        card("Вакансия: Python-разработчик в команду платформы"),
        profile(desired_role="Python-разработчик"),
    )
    assert outcome.score_0_100 > 0.0
    assert outcome.breakdown.get("desired_role", 0.0) > 0.0


def test_matching_is_case_insensitive_and_morphology_tolerant():
    """«разработчика» в тексте вакансии матчит «разработчик» из профиля."""
    outcome = resume_match_score(
        card("Ищем PYTHON-разработчика"),
        profile(skills=["python"], desired_role="разработчик"),
    )
    assert outcome.score_0_100 > 0.0


def test_substring_does_not_count_as_match():
    """Строгий токен-матч, как _name_matches в employer.py (#74 F4)."""
    outcome = resume_match_score(
        card("Требуется знание Go и гошных сервисов"),
        profile(skills=["Django"]),
    )
    assert outcome.score_0_100 == 0.0


# --- вырожденные входы -------------------------------------------------------


def test_empty_profile_scores_zero():
    outcome = resume_match_score(card("Требуется Python, Django"), profile())
    assert outcome.score_0_100 == 0.0


def test_none_profile_scores_zero():
    outcome = resume_match_score(card("Требуется Python, Django"), None)
    assert outcome.score_0_100 == 0.0


def test_empty_vacancy_text_scores_zero():
    """Нет текста — нет доказательства совпадения (fail-closed, не «идеальный матч»)."""
    outcome = resume_match_score(card(""), profile(skills=["Python", "Django"]))
    assert outcome.score_0_100 == 0.0


def test_both_empty_scores_zero_without_raising():
    outcome = resume_match_score(card(""), profile())
    assert outcome.score_0_100 == 0.0


# --- regression на класс ошибки #490 (тема vs намерение) ---------------------


def test_negated_requirement_is_not_counted_as_match():
    """#490: «без опыта Python» — тема совпала, намерение противоположное.

    Наивное пересечение токенов засчитало бы «python» как совпадение и подняло
    бы score вакансии, которая явно требует ОТСУТСТВИЯ навыка. Отрицание перед
    токеном снимает совпадение (fail-closed: лучше недосчитать, чем завысить).
    """
    outcome = resume_match_score(
        card("Ищем аналитика без опыта Python, обучение с нуля"),
        profile(skills=["Python"]),
    )
    assert outcome.score_0_100 == 0.0


def test_negation_does_not_suppress_other_matches_in_same_text():
    """Отрицание локально: гасит только свой токен, не весь текст вакансии."""
    negated = resume_match_score(
        card("Требуется Django; знание Python не требуется"),
        profile(skills=["Python", "Django"]),
    ).score_0_100
    clean = resume_match_score(
        card("Требуется Django и Python"),
        profile(skills=["Python", "Django"]),
    ).score_0_100
    assert 0.0 < negated < clean


# --- встраивание в rank_candidates (Этап 1: наблюдение без последствий) ------


def _resume_with_profile(ai_profile):
    """Минимальный stand-in ResumeConfig: rank_candidates читает поля через getattr."""

    class _Resume:
        pass

    resume = _Resume()
    resume.ai_profile = ai_profile
    resume.scoring = None
    return resume


def test_rank_candidates_logs_resume_match(caplog):
    """Score попадает в лог для наблюдения за распределением (#492 Этап 1)."""
    from hhru_bot.config import SearchFilters
    from hhru_bot.search import rank_candidates

    cards = [card("Требуется Python и Django", title="Python-разработчик")]
    with caplog.at_level("INFO", logger="hhru_bot.search"):
        rank_candidates(
            cards,
            SearchFilters(text="python"),
            _resume_with_profile(profile(skills=["Python", "Django"])),
        )

    assert any("resume-match" in record.message for record in caplog.records)


def test_rank_candidates_order_unchanged_by_resume_match():
    """Этап 1 не ранжирует и не отсеивает: порядок и состав те же, что без профиля."""
    from hhru_bot.config import SearchFilters
    from hhru_bot.search import rank_candidates

    cards = [
        VacancyCard("1", "Java-разработчик", "A", "u1", vacancy_text="Java, Spring"),
        VacancyCard("2", "Python-разработчик", "B", "u2", vacancy_text="Python, Django"),
    ]
    filters = SearchFilters(text="python")

    with_profile = rank_candidates(cards, filters, _resume_with_profile(profile(skills=["Python"])))
    without_profile = rank_candidates(cards, filters, _resume_with_profile(None))

    assert [c.vacancy_id for c, _, _ in with_profile] == [
        c.vacancy_id for c, _, _ in without_profile
    ]

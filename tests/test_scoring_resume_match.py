"""Тесты resume<->vacancy keyword-match скоринга (issue #492, Этап 1).

Чистая логика без браузера/LLM: score_resume_match взвешенно пересекает токены
AIProfile (skills/desired_role/highlights/summary) с VacancyCard.vacancy_text.
Покрывает: точное совпадение, частичное, полное несовпадение, пустой профиль,
пустой vacancy_text, и regression-кейс класса #490 (тема слова, а не намерение
— наивное пересечение не должно давать ложный высокий score на нерелевантной
роли только из-за совпадения общеупотребимых существительных).
"""

from __future__ import annotations

import pytest

from hhru_bot.config_sections.ai_profile import AIProfile
from hhru_bot.scoring import score_resume_match
from hhru_bot.search import VacancyCard

pytestmark = pytest.mark.unit


def _card(vacancy_text: str, title: str = "Vacancy") -> VacancyCard:
    return VacancyCard(
        vacancy_id="1",
        title=title,
        company="ООО Ромашка",
        url="https://hh.ru/vacancy/1",
        vacancy_text=vacancy_text,
    )


def test_exact_match_scores_high():
    profile = AIProfile(
        skills=["python", "django", "postgresql"],
        desired_role="backend developer",
    )
    card = _card(
        "Ищем Python-разработчика со знанием Django и PostgreSQL на позицию "
        "backend developer в нашу команду."
    )

    outcome = score_resume_match(card, profile)

    assert outcome.score_0_100 == pytest.approx(100.0)
    assert set(outcome.matched_tokens) >= {"python", "django", "postgresql", "backend", "developer"}


def test_partial_match_scores_between_bounds():
    profile = AIProfile(skills=["python", "django", "kubernetes"])
    card = _card("Ищем Python-разработчика, опыт с Django обязателен.")

    outcome = score_resume_match(card, profile)

    assert 0.0 < outcome.score_0_100 < 100.0


def test_no_overlap_scores_zero():
    profile = AIProfile(skills=["python", "django"], desired_role="backend developer")
    card = _card("Требуется сварщик пятого разряда на производство металлоконструкций.")

    outcome = score_resume_match(card, profile)

    assert outcome.score_0_100 == 0.0
    assert outcome.matched_tokens == ()


def test_empty_profile_returns_zero_with_reason():
    card = _card("Ищем Python-разработчика.")

    outcome = score_resume_match(card, None)

    assert outcome.score_0_100 == 0.0
    assert outcome.breakdown.get("reason") is not None


def test_blank_profile_fields_return_zero_with_reason():
    profile = AIProfile()  # все поля пустые по умолчанию
    card = _card("Ищем Python-разработчика.")

    outcome = score_resume_match(card, profile)

    assert outcome.score_0_100 == 0.0
    assert outcome.breakdown.get("reason") is not None


def test_empty_vacancy_text_returns_zero_with_reason():
    profile = AIProfile(skills=["python"])
    card = _card("")

    outcome = score_resume_match(card, profile)

    assert outcome.score_0_100 == 0.0
    assert outcome.breakdown.get("reason") is not None


def test_topic_word_without_intent_does_not_inflate_score():
    """Regression для класса ошибки #490: совпадение общей темы (не намерения).

    Профиль ищет роль HR-специалиста (тема "зарплата" как часть обязанностей).
    Вакансия — программист, где "зарплата" встречается только в блоке
    компенсации ("зарплата от..."). Наивное пересечение токенов ловит слово
    "зарплата" в обоих текстах, но это не должно давать высокий score: остальные
    специфичные токены профиля (hr, кадры, подбор) в вакансии отсутствуют, и
    единственное совпадение — по общеупотребимому слову с разным намерением.
    """
    profile = AIProfile(
        skills=["hr", "кадровое делопроизводство", "подбор персонала"],
        desired_role="hr-специалист, расчет зарплаты сотрудников",
    )
    card = _card(
        "Требуется Python-разработчик. Зарплата от 200000 руб. Обязанности: "
        "разработка backend-сервисов на Django."
    )

    outcome = score_resume_match(card, profile)

    assert outcome.score_0_100 < 30.0

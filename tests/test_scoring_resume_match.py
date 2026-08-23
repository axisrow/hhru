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
from hhru_bot.scoring.resume_match import NO_DATA_RATIONALE
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


@pytest.mark.parametrize(
    ("skill", "vacancy_text"),
    [
        # Латинские пары: нормализация их не трогает, поэтому общий префикс
        # никогда не должен давать совпадения.
        ("java", "Требуется JavaScript, React, Node.js"),
        ("react", "Ищем инженера по reactive streams"),
        ("scala", "Опыт scalable-архитектур"),
        # Русские пары: общее начало есть, но стемы разные.
        ("админ", "Администратор торгового зала, график 2/2"),
        ("план", "Планета развлечений приглашает аниматора"),
        ("банк", "Организация банкет под ключ"),
        ("курс", "Требуется курьер на личном авто"),
    ],
)
def test_unrelated_shared_prefix_does_not_count_as_match(skill, vacancy_text):
    """Общее начало != словоформа: «java» не должен матчить «javascript».

    Направление ошибки — ЗАВЫШЕНИЕ score, что противоречит fail-closed принципу
    модуля и загрязняет распределение, ради наблюдения за которым сделан Этап 1.
    """
    outcome = resume_match_score(card(vacancy_text), profile(skills=[skill]))
    assert outcome.score_0_100 == 0.0


def test_yo_is_folded_to_ye_before_matching():
    """«ё»/«е» — одно слово: hh.ru пишет его обоими способами.

    Без сворачивания «учёт» в профиле и «учета» в вакансии — разные токены, и
    совпадение терялось молча, ЗАНИЖАЯ score (обратное направление к дефекту
    префиксного матча, но такой же шум в распределении Этапа 1).
    """
    assert (
        resume_match_score(
            card("Ведение бухгалтерского учета"), profile(skills=["учёт"])
        ).score_0_100
        > 0.0
    )
    # И симметрично: «ё» в тексте вакансии против «е» в профиле.
    assert (
        resume_match_score(card("Найдём специалиста"), profile(skills=["найдем"])).score_0_100 > 0.0
    )


@pytest.mark.parametrize(
    ("skill", "vacancy_text"),
    [
        # ЗАМЕЩЕНИЕ окончания — основной случай русского словоизменения и ровно
        # тот, на котором сравнение по префиксу промахивалось при любой разнице
        # длин. Эти пары первичны: предыдущая версия теста содержала только
        # дописывание и потому подтверждала неверное допущение вместо проверки.
        ("разработка", "Разработки на Python"),
        ("разработка", "Участие в разработке сервисов"),
        ("тестирование", "Опыт в тестировании веб-приложений"),
        ("аналитика", "Работа в аналитике продукта"),
        ("автоматизация", "Задачи автоматизации процессов"),
        ("поддержка", "Помощь в поддержке пользователей"),
        ("администрирование", "Опыт в администрировании серверов"),
        # ДОПИСЫВАНИЕ окончания — второй случай, тоже обязан работать.
        ("разработчик", "Требуется разработчика в команду"),
        ("аналитик", "Ищем аналитика данных"),
        # Трёхбуквенный корень: держит _MIN_STEM_LEN сверху — при пороге 4
        # окончание уже не снимается и словоформа перестаёт матчиться.
        ("бот", "Разработка ботов для Telegram"),
        ("чат", "Поддержка чата с клиентами"),
    ],
)
def test_real_russian_word_forms_match(skill, vacancy_text):
    """Русская словоформа обязана матчиться — ради этого стемминг и введён.

    Страж от «починки», которая гасит ложные срабатывания ценой потери
    настоящих совпадений: обратное направление той же ошибки.
    """
    outcome = resume_match_score(card(vacancy_text), profile(skills=[skill]))
    assert outcome.score_0_100 > 0.0, f"{skill!r} должен матчить {vacancy_text!r}"


def test_affirmative_ne_tolko_is_not_read_as_negation():
    """«не только Python, но и SQL» — конструкция УТВЕРДИТЕЛЬНАЯ.

    Маркер «не» здесь не отрицает навык, а вводит перечисление. Без отдельной
    проверки частотная формулировка вакансий гасила бы верное совпадение.
    """
    outcome = resume_match_score(
        card("Нужен не только Python, но и SQL"),
        profile(skills=["Python"]),
    )
    assert outcome.score_0_100 > 0.0


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


def test_no_data_zero_is_distinguishable_from_real_mismatch():
    """Оба нуля на одной шкале — различает только rationale (#492 Этап 1).

    Без этого распределение, по которому калибруется порог Этапа 2, смешало бы
    «нечего было считать» с «честно не совпало» в одном бакете.
    """
    no_data = resume_match_score(card(""), profile(skills=["Python"]))
    mismatch = resume_match_score(card("Требуется 1С и УТ 11"), profile(skills=["Python"]))

    assert no_data.score_0_100 == mismatch.score_0_100 == 0.0
    assert no_data.rationale == NO_DATA_RATIONALE
    assert mismatch.rationale != NO_DATA_RATIONALE


def test_empty_profile_is_marked_as_no_data():
    assert resume_match_score(card("Требуется Python"), profile()).rationale == NO_DATA_RATIONALE
    assert resume_match_score(card("Требуется Python"), None).rationale == NO_DATA_RATIONALE


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


def test_rank_candidates_survives_resume_match_failure(monkeypatch, caplog):
    """Сбой наблюдения не роняет план откликов (#492 Этап 1).

    ``_log_resume_match`` вызывается ВНУТРИ ``rank_candidates``, поэтому
    необработанное исключение оборвало бы весь apply ради строчки в логе.
    Ранжирование обязано доработать и вернуть полный состав кандидатов.
    """
    from hhru_bot import scoring as scoring_pkg
    from hhru_bot import search as search_mod
    from hhru_bot.config import SearchFilters

    def _boom(card, profile):
        raise RuntimeError("scoring exploded")

    # Импорт в _log_resume_match ленивый (``from .scoring import ...``), поэтому
    # подменять надо атрибут пакета — он резолвится в момент вызова.
    monkeypatch.setattr(scoring_pkg, "resume_match_score", _boom)

    cards = [
        VacancyCard("1", "Python-разработчик", "A", "u1", vacancy_text="Python"),
        VacancyCard("2", "Java-разработчик", "B", "u2", vacancy_text="Java"),
    ]
    with caplog.at_level("WARNING", logger="hhru_bot.search"):
        ranked = search_mod.rank_candidates(
            cards,
            SearchFilters(text="python"),
            _resume_with_profile(profile(skills=["Python"])),
        )

    assert [c.vacancy_id for c, _, _ in ranked] == ["1", "2"]
    assert any("resume-match failed" in record.message for record in caplog.records)

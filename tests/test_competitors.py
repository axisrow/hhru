from __future__ import annotations

import pytest

from hhru_bot.competitors import (
    CompetitorResumeIndeterminate,
    build_competitor_search_url,
    parse_competitor_resume_text,
    parse_search_links,
    redact_free_text,
    report_competitors,
)

pytestmark = pytest.mark.unit


DETAIL = """
Был сегодня
Мужчина
Активно ищет работу
Москва, готов работать удалённо
AI Engineer / AI Infrastructure Engineer
200 000 ₽ на руки
Специализации:
— Программист, разработчик
— Системный инженер
Тип занятости: полная занятость, проектная работа/разовое задание
Формат работы: удалённо, гибрид
Опыт работы 5 лет 3 месяца
Навыки
Уровни владения навыками
Продвинутый уровень
Python
Docker
Средний уровень
RAG
Базовый уровень
FastAPI
Образование
Высшее образование
Знание языков
Русский — Родной
Английский — B2 — Средне-продвинутый
Гражданство, время в пути до работы
Желательное время в пути до работы: Не имеет значения
"""


def test_search_url_is_keyword_only_and_page_numbered():
    url = build_competitor_search_url("AI Engineer", 2)
    assert "text=AI+Engineer" in url
    assert "page=2" in url
    assert "resume=" not in url


def test_parse_search_links_normalizes_url_deduplicates_and_keeps_rank():
    rows = [
        ("/resume/abc?query=AI", " AI Engineer "),
        ("/resume/abc?other=1", "duplicate"),
        ("/vacancy/123", "not a resume"),
        ("https://hh.ru/resume/def", "AI Creator"),
    ]
    cards = parse_search_links(rows, rank_offset=20)
    assert [(card.resume_id, card.desired_role, card.rank) for card in cards] == [
        ("abc", "AI Engineer", 21),
        ("def", "AI Creator", 22),
    ]
    assert cards[0].resume_url == "https://hh.ru/resume/abc"


def test_parse_detail_extracts_only_competitor_fields():
    snapshot = parse_competitor_resume_text(
        DETAIL,
        resume_id="abc",
        resume_url="https://hh.ru/resume/abc",
        headings=[
            "AI Engineer / AI Infrastructure Engineer",
            "200 000 ₽ на руки",
            "Опыт работы 5 лет 3 месяца",
            "Навыки",
            "Образование",
            "Знание языков",
        ],
    )
    assert snapshot.desired_role == "AI Engineer / AI Infrastructure Engineer"
    assert snapshot.salary_from == 200_000
    assert snapshot.salary_to == 200_000
    assert snapshot.salary_currency == "RUB"
    assert snapshot.experience_months == 63
    assert snapshot.specializations == ["Программист, разработчик", "Системный инженер"]
    assert snapshot.employment_types == [
        "полная занятость",
        "проектная работа/разовое задание",
    ]
    assert [(skill.name, skill.proficiency) for skill in snapshot.skills] == [
        ("Python", "Продвинутый уровень"),
        ("Docker", "Продвинутый уровень"),
        ("RAG", "Средний уровень"),
        ("FastAPI", "Базовый уровень"),
    ]
    # Header demographics and location are never fields on the DTO.
    assert "Москва" not in snapshot.content_hash()


def test_detail_without_confirmed_role_fails_closed():
    with pytest.raises(CompetitorResumeIndeterminate, match="desired_role"):
        parse_competitor_resume_text(
            "Навыки\nPython",
            resume_id="abc",
            resume_url="https://hh.ru/resume/abc",
            headings=["Навыки"],
        )


def test_numeric_role_title_is_not_misparsed_as_salary():
    snapshot = parse_competitor_resume_text(
        "3D Generalist - AI Generalist\nОпыт работы 4 года\nНавыки\nCinema 4D",
        resume_id="3d",
        resume_url="https://hh.ru/resume/3d",
        headings=["3D Generalist - AI Generalist", "Опыт работы 4 года", "Навыки"],
    )
    assert snapshot.desired_role == "3D Generalist - AI Generalist"
    assert snapshot.salary_from is None


def test_thin_space_salary_and_dashless_specialization_are_normalized():
    snapshot = parse_competitor_resume_text(
        "AI Engineer\n2\u2009500\u00a0€ на\u00a0руки\nСпециализации:\nРазработчик\n"
        "Тип занятости: полная занятость\nОпыт работы 1\u00a0год",
        resume_id="thin",
        resume_url="https://hh.ru/resume/thin",
        headings=["AI Engineer", "2\u2009500\u00a0€ на\u00a0руки", "Опыт работы 1\u00a0год"],
    )
    assert snapshot.salary_from == 2500
    assert snapshot.salary_currency == "EUR"
    assert snapshot.specializations == ["Разработчик"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Пишите test@example.com или +7 999 123-45-67", "Пишите [redacted] или [redacted]"),
        ("Меня зовут Иван", None),
        ("Связаться с Иван Петров", None),
        ("Построил RAG-поиск и сократил latency", "Построил RAG-поиск и сократил latency"),
    ],
)
def test_redact_free_text(raw, expected):
    assert redact_free_text(raw) == expected


def test_report_is_deterministic_and_warns_about_limited_coverage():
    rows = [
        {
            "desired_role": "AI Engineer",
            "specializations": ["Разработчик"],
            "skills": [{"name": "Python"}, {"name": "RAG"}],
            "experience_months": 60,
            "salary_to": 200_000,
            "salary_currency": "RUB",
        },
        {
            "desired_role": "AI Engineer",
            "specializations": ["Разработчик"],
            "skills": [{"name": "Python"}],
            "experience_months": 24,
            "salary_to": 100_000,
            "salary_currency": "RUB",
        },
    ]
    report = report_competitors(rows, top=10, limited_runs=1)
    assert "Резюме в выборке: 2" in report
    assert "ограниченных запусков" in report
    assert "2  AI Engineer" in report
    assert "2  Python" in report
    assert "1  Python + RAG" in report
    assert "RUB: 200000 (n=2)" in report
    assert "Добавляйте навык только если он подтверждён" in report

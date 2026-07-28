"""Тесты regex-fallback извлечения ЗП из HTML карточки (issue #73).

Чистая логика без браузера: extract_salary_text_from_html() на HTML-фикстурах
из живого дампа hh.ru (magritte-разметка). Парсер parse_salary() покрыт
отдельным test_salary_parse.py и здесь не дублируется.
"""

from __future__ import annotations

from pathlib import Path

from hhru_bot.search import (
    extract_salary_text_from_html,
    parse_salary,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- извлечение текста ЗП из HTML ------------------------------------------


def test_extract_salary_from_card_with_salary():
    html = _load("vacancy_card_with_salary.html")
    text = extract_salary_text_from_html(html)
    assert text is not None
    assert "150" in text
    assert "руб" in text


def test_extract_salary_from_card_no_salary_returns_none():
    html = _load("vacancy_card_no_salary.html")
    assert extract_salary_text_from_html(html) is None


def test_extract_salary_from_usd_card():
    html = _load("vacancy_card_salary_usd.html")
    text = extract_salary_text_from_html(html)
    assert text is not None
    assert "$" in text or "USD" in text.lower()


def test_extract_salary_full_pipeline_with_salary():
    """extract_salary_text_from_html -> parse_salary = SalaryInfo."""
    html = _load("vacancy_card_with_salary.html")
    text = extract_salary_text_from_html(html)
    result = parse_salary(text)
    assert result is not None
    assert result.salary_from == 150000
    assert result.salary_to == 200000
    assert result.currency == "RUB"


def test_extract_salary_full_pipeline_no_salary():
    """Вакансия без ЗП -> parse_salary(None) -> None."""
    html = _load("vacancy_card_no_salary.html")
    text = extract_salary_text_from_html(html)
    assert text is None
    assert parse_salary(text) is None


def test_extract_salary_full_pipeline_usd():
    html = _load("vacancy_card_salary_usd.html")
    text = extract_salary_text_from_html(html)
    result = parse_salary(text)
    assert result is not None
    assert result.salary_from == 3000
    assert result.salary_to is None
    assert result.currency == "USD"


def test_extract_salary_inline_text():
    """Regex работает и на голом тексте (не только HTML)."""
    text = extract_salary_text_from_html(
        '<div class="magritte-serp-item__salary">'
        '<span class="magritte-text">от 80 000 ₽</span></div>'
    )
    assert text is not None
    result = parse_salary(text)
    assert result is not None
    assert result.salary_from == 80000
    assert result.salary_to is None


def test_extract_salary_rejects_no_currency():
    """Числа без валюты не матчат regex (ложные '50 вакансий')."""
    html = "<div>50 вакансий · 3 000 отзывов · Москва</div>"
    assert extract_salary_text_from_html(html) is None


def test_extract_salary_rejects_oversized():
    """Числа > 50 000 000 отсекаются (KZT-зарплаты до 50M валидны)."""
    html = "<div>100 000 000 руб.</div>"
    assert extract_salary_text_from_html(html) is None


def test_extract_salary_empty_html():
    assert extract_salary_text_from_html("") is None


def test_extract_salary_only_employer():
    """Карточка с только названием компании без ЗП."""
    html = (
        '<div data-qa="vacancy-serp__vacancy">'
        '<a data-qa="serp-item__title">Developer</a>'
        '<span data-qa="vacancy-serp__vacancy-employer">Corp</span>'
        "</div>"
    )
    assert extract_salary_text_from_html(html) is None


def test_extract_salary_strips_html_tags():
    """Результат extract не содержит HTML-тегов (raw в SalaryInfo чистый)."""
    html = '<div><span class="mt">150 000–200 000 руб.</span></div>'
    text = extract_salary_text_from_html(html)
    assert text is not None
    assert "<span" not in text
    assert "<div" not in text
    result = parse_salary(text)
    assert result is not None
    assert result.raw == text


def test_extract_salary_boundary_min():
    """1 000 руб. -- ровно на нижней границе, валидно."""
    html = "<div>1 000 руб.</div>"
    text = extract_salary_text_from_html(html)
    assert text is not None


def test_extract_salary_boundary_max():
    """50 000 000 руб. -- ровно на верхней границе, валидно."""
    html = "<div>50 000 000 руб.</div>"
    text = extract_salary_text_from_html(html)
    assert text is not None


def test_extract_salary_below_min():
    """999 руб. -- ниже нижней границы, отсекается."""
    html = "<div>999 руб.</div>"
    assert extract_salary_text_from_html(html) is None

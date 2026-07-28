"""Тесты regex-fallback извлечения ЗП из HTML карточки (issue #73).

Чистая логика без браузера: extract_salary_text_from_html() на HTML-фикстурах
из живого дампа hh.ru (magritte-разметка). Парсер parse_salary() покрыт
отдельным test_salary_parse.py и здесь не дублируется.
"""

from __future__ import annotations

from pathlib import Path

from hhru_bot.search import (
    extract_salary_text,
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
    # Валюта рублей в magritte-разметке -- символ ₽ или слово «руб».
    assert "₽" in text or "руб" in text


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


def test_extract_salary_split_across_spans():
    """Регрессия #78: magritte разбивает ЗП по spans.

    Живая разметка hh.ru: «<span>150</span><span> </span><span>000</span>
    <span>₽</span>» — теги внутри числа разрывают матч «digits+валюта подряд»,
    и регексп по СЫРОЙ HTML даёт None. Фикс: regex применяется к тексту без
    тегов. До фикса этот тест падал (salary=NULL у всех вакансий).
    """
    html = (
        '<div class="magritte-serp-item__salary">'
        '<span class="magritte-text__tg3gq5">'
        "<span>150</span><span> </span><span>000</span>"
        "<span>–</span>"
        "<span>200</span><span> </span><span>000</span>"
        "<span> </span><span>₽</span>"
        "</span></div>"
    )
    text = extract_salary_text_from_html(html)
    assert text is not None
    assert "<span" not in text
    result = parse_salary(text)
    assert result is not None
    assert result.salary_from == 150000
    assert result.salary_to == 200000
    assert result.currency == "RUB"


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


# --- #93: textContent (inner_text) вместо innerHTML ---------------------------
#
# Аудит #93: hh.ru для части вакансий отдаёт ЗП только в textContent
# (card.inner_text()), а в innerHTML блока нет — регексп по innerHTML пропускал
# такие ЗП (5/15 → 19/20). extract_salary_text кормится именно textContent.


def test_extract_salary_from_inner_text_text_content():
    """textContent из inner_text() — ЗП ловится, как и из HTML без тегов (#93)."""
    text = "Senior Backend Developer\nООО Технологии\nсегодня\nот 350 000 ₽\nМосква"
    salary_text = extract_salary_text(text)
    assert salary_text is not None
    result = parse_salary(salary_text)
    assert result is not None
    assert result.salary_from == 350000
    assert result.salary_to is None
    assert result.currency == "RUB"


def test_extract_salary_inner_text_fixture_html_has_no_salary():
    """Регрессия #93 (часть A): в HTML фикстуры text-only ЗП нет совсем —
    innerHTML-regex НЕ находит (моделирует JS-рендер ЗП в textContent)."""
    html = _load("vacancy_card_text_only_salary.html")
    assert extract_salary_text(html) is None


def test_extract_salary_inner_text_fixture_text_has_salary():
    """Регрессия #93 (часть A): textContent (inner_text) той же карточки несёт
    ЗП — extract_salary_text её находит. Это и есть суть фикса: меняя источник
    innerHTML→inner_text в search_vacancies, ловим ранее пропущенные ЗП."""
    text = _load("vacancy_card_text_only_salary.txt")
    salary_text = extract_salary_text(text)
    assert salary_text is not None
    result = parse_salary(salary_text)
    assert result is not None
    assert result.salary_from == 350000


def test_extract_salary_alias_delegates():
    """extract_salary_text_from_html — deprecated-алиас, делегирует в новое имя."""
    assert extract_salary_text("от 80 000 ₽") == extract_salary_text_from_html("от 80 000 ₽")


def test_extract_salary_text_rejects_no_currency():
    """Ложные срабатывания на textContent без валюты отсекаются."""
    text = "Backend Developer\nООО Ромашка\nМосква\n3 000 отзывов\n50 вакансий"
    assert extract_salary_text(text) is None

"""ТДД-тесты чистого парсера зарплаты hh.ru (issue #14).

Парсер `parse_salary(text)` разбирает строку вида
'150 000–200 000 руб.' / 'от 80 000 ₽' / 'до 120 000 USD' / '3 000–5 000 бел. руб'
в структуру SalaryInfo, либо возвращает None для 'з/п не указана' и пустоты.

Тесты написаны ПЕРЕД реализацией (красная фаза ТДД). Edge-cases покрывают
диапазоны, фиксированные значения, приставки от/до, разные валюты и
текстовые заглушки hh.ru.
"""

from __future__ import annotations

import pytest

from hhru_bot.search import SalaryInfo, parse_salary

pytestmark = pytest.mark.unit

# --- отсутствие зарплаты ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "з/п не указана",
        "З/п не указана",
        "",
        "   ",
        None,
    ],
)
def test_parse_salary_none_when_missing(text):
    assert parse_salary(text) is None


# --- фиксированное значение -------------------------------------------------


def test_parse_salary_fixed_rub():
    result = parse_salary("150 000 руб.")
    assert result == SalaryInfo(
        salary_from=150000, salary_to=150000, currency="RUB", raw="150 000 руб."
    )


def test_parse_salary_fixed_with_symbol_currency():
    # hh.ru использует символ ₽, парсер нормализует в RUB
    result = parse_salary("100000 ₽")
    assert result is not None
    assert result.salary_from == 100000
    assert result.salary_to == 100000
    assert result.currency == "RUB"


# --- диапазон ---------------------------------------------------------------


def test_parse_salary_range_rub():
    result = parse_salary("150 000–200 000 руб.")
    assert result == SalaryInfo(
        salary_from=150000, salary_to=200000, currency="RUB", raw="150 000–200 000 руб."
    )


def test_parse_salary_range_usd():
    result = parse_salary("3 000 – 5 000 USD")
    assert result is not None
    assert result.salary_from == 3000
    assert result.salary_to == 5000
    assert result.currency == "USD"


# --- от / до ----------------------------------------------------------------


def test_parse_salary_from_only():
    result = parse_salary("от 80 000 ₽")
    assert result is not None
    assert result.salary_from == 80000
    assert result.salary_to is None
    assert result.currency == "RUB"


def test_parse_salary_to_only():
    result = parse_salary("до 120 000 руб.")
    assert result is not None
    assert result.salary_from is None
    assert result.salary_to == 120000
    assert result.currency == "RUB"


# --- валюты -----------------------------------------------------------------


def test_parse_salary_eur():
    result = parse_salary("2 000–3 500 EUR")
    assert result is not None
    assert result.currency == "EUR"


def test_parse_salary_kzt():
    result = parse_salary("300 000 – 500 000 KZT")
    assert result is not None
    assert result.currency == "KZT"


def test_parse_salary_bel_rub():
    # «бел. руб» — словесная валюта с точкой
    result = parse_salary("3 000–5 000 бел. руб")
    assert result is not None
    assert result.salary_from == 3000
    assert result.salary_to == 5000
    assert result.currency == "BYN"


# --- устойчивость к мусору / разделителям -----------------------------------


def test_parse_salary_nbsp_separator():
    # hh.ru использует неразрывные пробелы (U+00A0 / U+202F) как разделители разрядов
    result = parse_salary("150 000 ₽")
    assert result is not None
    assert result.salary_from == 150000


def test_parse_salary_unknown_currency_fallback():
    # Незнакомая валюта — сохраняем исходную строку валюты, но парсер не падает
    result = parse_salary("5 000–7 000 ₸")
    assert result is not None
    assert result.salary_from == 5000
    assert result.salary_to == 7000


def test_parse_salary_returns_raw_original():
    text = "150 000–200 000 руб."
    result = parse_salary(text)
    assert result is not None
    assert result.raw == text


# --- структура --------------------------------------------------------------


def test_salary_info_repr_contains_raw():
    si = SalaryInfo(salary_from=100, salary_to=200, currency="RUB", raw="100–200 руб.")
    assert "100" in repr(si)

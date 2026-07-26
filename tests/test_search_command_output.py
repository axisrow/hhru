"""Тесты форматирования вывода команды search (issue #14).

Проверяем, что новые поля salary/raw_date рендерятся в строку карточки
аккуратно: присутствуют когда есть, отсутствуют (без пустых скобок) когда нет.
Без браузера — чистые функции _format_salary / _format_card_line.
"""

from __future__ import annotations

from hhru_bot.commands.search import _format_card_line, _format_salary
from hhru_bot.search import SalaryInfo, VacancyCard


def _card(salary=None, raw_date=None, title="Dev", company="Acme", url="https://hh.ru/vacancy/1"):
    return VacancyCard(
        vacancy_id="1", title=title, company=company, url=url, salary=salary, raw_date=raw_date
    )


# --- _format_salary ---


def test_format_salary_none_empty():
    assert _format_salary(None) == ""


def test_format_salary_range():
    s = SalaryInfo(150000, 200000, "RUB", "raw")
    assert _format_salary(s) == "150000-200000 RUB"


def test_format_salary_from_only():
    s = SalaryInfo(80000, None, "RUB", "raw")
    assert _format_salary(s) == "от 80000 RUB"


def test_format_salary_to_only():
    s = SalaryInfo(None, 120000, "USD", "raw")
    assert _format_salary(s) == "до 120000 USD"


def test_format_salary_no_bounds_empty():
    # Защитный случай: оба None (аномальный SalaryInfo) → пустая строка
    s = SalaryInfo(None, None, "RUB", "raw")
    assert _format_salary(s) == ""


# --- _format_card_line ---


def test_card_line_without_salary_or_date_is_plain():
    line = _format_card_line(_card())
    assert line == "Dev — Acme (https://hh.ru/vacancy/1)"


def test_card_line_with_salary_only():
    salary = SalaryInfo(100000, 100000, "RUB", "raw")
    line = _format_card_line(_card(salary=salary))
    assert "| 100000 RUB" in line
    # Без пустой даты/скобок
    assert " / " not in line


def test_card_line_with_date_only():
    line = _format_card_line(_card(raw_date="2 дня назад"))
    assert "| 2 дня назад" in line


def test_card_line_with_salary_and_date():
    salary = SalaryInfo(150000, 200000, "RUB", "raw")
    line = _format_card_line(_card(salary=salary, raw_date="сегодня"))
    assert "150000-200000 RUB / сегодня" in line

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


# --- запись собранных карточек в рынок (#66) ---------------------------------
#
# search СОБИРАЕТ карточки (VacancyCard с salary, #34), но НЕ писал их в БД —
# рынок-анализ был не из чего строить. _record_seen = побочный эффект сбора:
# пишет ВСЕ собранные карточки в vacancies_seen, не трогая отбор/скоринг/вывод.


def test_record_seen_writes_all_cards(tmp_path):
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.history import History

    history = History(tmp_path / "h.db")
    cards = [
        VacancyCard(
            vacancy_id="1",
            title="Backend",
            company="Yandex",
            url="https://hh.ru/vacancy/1",
            salary=SalaryInfo(300000, 400000, "RUB", "raw"),
            raw_date="сегодня",
        ),
        VacancyCard(
            vacancy_id="2",
            title="DevOps",
            company="Acme",
            url="https://hh.ru/vacancy/2",
            salary=None,
            raw_date=None,
        ),
    ]
    _record_seen(cards, "python backend", history)

    rows = history.list_vacancies_seen()
    assert len(rows) == 2
    by_id = {r["vacancy_id"]: r for r in rows}
    assert by_id["1"]["salary_from"] == 300000
    assert by_id["1"]["search_query"] == "python backend"
    # вакансия без зарплаты тоже записана
    assert by_id["2"]["salary_from"] is None


def test_record_seen_failure_does_not_raise(tmp_path):
    """Сбой записи НЕ должен валить поиск — рынок лишь удобство."""
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.history import History

    history = History(tmp_path / "h.db")

    def _boom(**_kwargs):
        raise RuntimeError("boom")

    history.upsert_vacancy_seen = _boom  # type: ignore[method-assign]
    cards = [VacancyCard(vacancy_id="1", title="T", company="C", url="https://hh.ru/vacancy/1")]
    _record_seen(cards, "python", history)  # не должно упасть

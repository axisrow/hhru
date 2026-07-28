"""Тесты таблицы vacancies_seen и агрегатов рынка (#66, Этап 1).

search СОБИРАЕТ карточки с зарплатой (#34), но раньше НЕ писал их в БД — рынок
был не из чего анализировать. vacancies_seen = побочный эффект сбора: одна
строка на (vacancy_id, search_query), upsert по свежему scrape (обновляет
зарплату/дату, двигает last_seen_at). Без браузера — только SQLite.
"""

from __future__ import annotations

import sqlite3

from hhru_bot.history import History

# --- upsert ------------------------------------------------------------------


def test_upsert_inserts_new_vacancy(tmp_path):
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="Backend",
        company="Yandex",
        salary_from=300000,
        salary_to=400000,
        salary_currency="RUB",
        raw_date="30 июля",
        search_query="python backend",
    )
    rows = h.list_vacancies_seen()
    assert len(rows) == 1
    row = rows[0]
    assert row["vacancy_id"] == "123"
    assert row["title"] == "Backend"
    assert row["company"] == "Yandex"
    assert row["salary_from"] == 300000
    assert row["salary_to"] == 400000
    assert row["salary_currency"] == "RUB"
    assert row["raw_date"] == "30 июля"
    assert row["search_query"] == "python backend"
    assert row["first_seen_at"] is not None
    assert row["last_seen_at"] is not None


def test_upsert_updates_existing_vacancy_keeps_first_seen(tmp_path):
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(vacancy_id="123", title="Old", company="X", search_query="python")
    first_seen = h.list_vacancies_seen()[0]["first_seen_at"]

    # Свежий scrape: зарплата появилась, заголовок/компания обновились.
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="New",
        company="Y",
        salary_from=350000,
        salary_to=350000,
        salary_currency="RUB",
        search_query="python",
    )
    rows = h.list_vacancies_seen()
    assert len(rows) == 1  # НЕ задвоилось
    row = rows[0]
    assert row["title"] == "New"
    assert row["company"] == "Y"
    assert row["salary_from"] == 350000
    assert row["first_seen_at"] == first_seen  # first_seen не двигается
    assert row["last_seen_at"] >= first_seen


def test_upsert_same_vacancy_different_query_keeps_both(tmp_path):
    """UNIQUE(vacancy_id, search_query): одна вакансия по разным запросам —
    отдельные строки (рынок хочет видеть, по каким запросам что находится)."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(vacancy_id="123", title="T", company="C", search_query="python")
    h.upsert_vacancy_seen(vacancy_id="123", title="T", company="C", search_query="django")
    rows = h.list_vacancies_seen()
    assert len(rows) == 2


def test_upsert_accepts_null_salary(tmp_path):
    """Вакансия без зарплаты (parse_salary → None) тоже пишется — для полноты
    картины рынка (доля без зарплаты по сфере)."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="No salary",
        company="C",
        salary_from=None,
        salary_to=None,
        salary_currency=None,
        raw_date=None,
        search_query="python",
    )
    row = h.list_vacancies_seen()[0]
    assert row["salary_from"] is None
    assert row["salary_to"] is None
    assert row["salary_currency"] is None


# --- агрегаты рынка (медиана по сфере) ---------------------------------------


def test_market_salary_by_query_returns_median(tmp_path):
    """Главная цель #66: сравнение сфер по медианной ЗП. Медиана берётся по
    salary_to (верхняя граница диапазона / фикс. значение) — отражает потолок
    предложения, а не заниженную нижнюю границу «от N»."""
    h = History(tmp_path / "h.db")
    # python: 100, 200, 300, 400, 500 → медиана 300
    for i, s in enumerate([100, 200, 300, 400, 500]):
        h.upsert_vacancy_seen(
            vacancy_id=f"p{i}",
            title="P",
            company="C",
            salary_from=s,
            salary_to=s,
            salary_currency="RUB",
            search_query="python",
        )
    # performance: 100, 150, 200 → медиана 150
    for i, s in enumerate([100, 150, 200]):
        h.upsert_vacancy_seen(
            vacancy_id=f"m{i}",
            title="M",
            company="C",
            salary_from=s,
            salary_to=s,
            salary_currency="RUB",
            search_query="performance",
        )

    rows = h.market_salary_by_query()
    by_q = {r["search_query"]: r for r in rows}

    assert by_q["python"]["median_to"] == 300
    assert by_q["python"]["count"] == 5
    assert by_q["performance"]["median_to"] == 150
    assert by_q["performance"]["count"] == 3


def test_market_salary_ignores_null_salary_in_median(tmp_path):
    """Вакансии без указанной ЗП учитываются в count, но в медиану не идут."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(vacancy_id="1", title="T", company="C", search_query="python")
    h.upsert_vacancy_seen(
        vacancy_id="2",
        title="T",
        company="C",
        salary_from=100,
        salary_to=100,
        salary_currency="RUB",
        search_query="python",
    )
    h.upsert_vacancy_seen(
        vacancy_id="3",
        title="T",
        company="C",
        salary_from=300,
        salary_to=300,
        salary_currency="RUB",
        search_query="python",
    )
    rows = h.market_salary_by_query()
    row = rows[0]
    assert row["count"] == 3  # все три
    assert row["median_to"] == 200  # медиана по двум с зарплатой
    # count без зарплаты в отдельном поле — для понимания полноты данных
    assert row["with_salary"] == 2


def test_market_salary_empty_when_no_data(tmp_path):
    h = History(tmp_path / "h.db")
    assert h.market_salary_by_query() == []


def test_market_salary_sorted_by_median_desc(tmp_path):
    """Сферы с ВЫШЕ доходом — наверху: это и есть цель «максимизация дохода»,
    выгода должна бросаться в глаза первой."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        salary_from=100,
        salary_to=100,
        salary_currency="RUB",
        search_query="low",
    )
    h.upsert_vacancy_seen(
        vacancy_id="2",
        title="T",
        company="C",
        salary_from=500,
        salary_to=500,
        salary_currency="RUB",
        search_query="high",
    )
    rows = h.market_salary_by_query()
    assert rows[0]["search_query"] == "high"
    assert rows[1]["search_query"] == "low"


def test_market_salary_even_count_averages_two_middle(tmp_path):
    """Чётное число значений: медиана = среднее двух центральных (как в SQLite
    percentile через AVG двух центральных строк)."""
    h = History(tmp_path / "h.db")
    # 100, 200, 300, 400 → медиана = (200+300)/2 = 250
    for i, s in enumerate([100, 200, 300, 400]):
        h.upsert_vacancy_seen(
            vacancy_id=f"v{i}",
            title="T",
            company="C",
            salary_from=s,
            salary_to=s,
            salary_currency="RUB",
            search_query="python",
        )
    rows = h.market_salary_by_query()
    assert rows[0]["median_to"] == 250


# --- list_vacancies_seen ----------------------------------------------------


def test_list_vacancies_seen_orders_recent_first(tmp_path):
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(vacancy_id="old", title="T", company="C", search_query="python")
    h.upsert_vacancy_seen(vacancy_id="new", title="T", company="C", search_query="python")
    rows = h.list_vacancies_seen()
    # свежий last_seen_at — первым
    assert rows[0]["vacancy_id"] == "new"


def test_market_aggregates_visible_to_readonly_query(tmp_path):
    """query (#45) открывает БД в read-only: таблица должна быть доступна
    произвольному SELECT без прав на запись."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        salary_from=100,
        salary_to=100,
        salary_currency="RUB",
        search_query="python",
    )
    conn = sqlite3.connect(f"file:{tmp_path / 'h.db'}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT search_query, COUNT(*) AS n FROM vacancies_seen GROUP BY search_query"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "python"
    assert row[1] == 1

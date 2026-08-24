"""Тесты таблицы vacancies_seen и агрегатов рынка (#66, Этап 1).

search СОБИРАЕТ карточки с зарплатой (#34), но раньше НЕ писал их в БД — рынок
был не из чего анализировать. vacancies_seen = побочный эффект сбора: одна
строка на (vacancy_id, search_query), upsert по свежему scrape (обновляет
зарплату, двигает last_seen_at). Без браузера — только SQLite.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from hhru_bot.history import History

pytestmark = pytest.mark.unit

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


def test_upsert_stores_extra_card_fields(tmp_path):
    """Доп. признаки карточки для статистики/ML (#517): address, is_remote,
    experience, snippet_requirement, snippet_responsibility, side_job, no_resume."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="Backend",
        company="Yandex",
        search_query="python backend",
        address="Москва",
        is_remote=True,
        experience="between1And3",
        snippet_requirement="Опыт работы с Hadoop.",
        snippet_responsibility="Развитие сервисов платформы.",
        side_job=True,
        no_resume=True,
        activity="Активно отвечает",
        hh_rating="4,8",
        hrbrand_winner=True,
        metro_stations='["Таганская", "Марксистская"]',
    )
    row = h.list_vacancies_seen()[0]
    assert row["address"] == "Москва"
    assert row["is_remote"] == 1
    assert row["experience"] == "between1And3"
    assert row["snippet_requirement"] == "Опыт работы с Hadoop."
    assert row["snippet_responsibility"] == "Развитие сервисов платформы."
    assert row["side_job"] == 1
    assert row["no_resume"] == 1
    assert row["activity"] == "Активно отвечает"
    assert row["hh_rating"] == "4,8"
    assert row["hrbrand_winner"] == 1
    assert row["metro_stations"] == '["Таганская", "Марксистская"]'


def test_upsert_extra_card_fields_default_to_null(tmp_path):
    """Опциональные блоки карточки — не роняем upsert, если их не передали."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(vacancy_id="123", title="T", company="C", search_query="python")
    row = h.list_vacancies_seen()[0]
    assert row["address"] is None
    assert row["is_remote"] is None
    assert row["experience"] is None
    assert row["snippet_requirement"] is None
    assert row["snippet_responsibility"] is None
    assert row["side_job"] is None
    assert row["no_resume"] is None
    assert row["activity"] is None
    assert row["hh_rating"] is None
    assert row["hrbrand_winner"] is None
    assert row["metro_stations"] is None


def test_empty_metro_snapshot_is_persisted_and_missing_snapshot_is_preserved(tmp_path):
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(vacancy_id="123", search_query="python", metro_stations='["Динамо"]')
    h.upsert_vacancy_seen(vacancy_id="123", search_query="python", metro_stations="[]")
    assert h.list_vacancies_seen()[0]["metro_stations"] == "[]"

    h.upsert_vacancy_seen(vacancy_id="123", search_query="python", metro_stations=None)
    assert h.list_vacancies_seen()[0]["metro_stations"] == "[]"


def test_upsert_refreshes_extra_card_fields_with_new_nonempty_value(tmp_path):
    """При повторном scrape новое НЕпустое значение address перезаписывает
    старое (карточка могла реально поменять город/формат работы)."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="T",
        company="C",
        search_query="python",
        address="Москва",
        is_remote=False,
    )
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="T",
        company="C",
        search_query="python",
        address="Санкт-Петербург",
        is_remote=True,
    )
    row = h.list_vacancies_seen()[0]
    assert row["address"] == "Санкт-Петербург"
    assert row["is_remote"] == 1


def test_upsert_keeps_previous_text_fields_when_scrape_misses_block(tmp_path):
    """address/experience/snippet_* опциональны в разметке hh.ru: пропуск
    блока при повторном scrape (транзиентный DOM-промах) НЕ должен затирать
    ранее собранное значение NULL'ом — COALESCE, как у published_at.
    is_remote — тристейтный сигнал: None означает «не удалось прочитать», а
    явный False остаётся валидным наблюдением."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="T",
        company="C",
        search_query="python",
        address="Москва",
        is_remote=True,
        experience="between1And3",
        snippet_requirement="req",
        snippet_responsibility="resp",
        side_job=True,
        no_resume=True,
    )
    # Повторный scrape: карточка не найдена/блоки не отрендерились —
    # caller шлёт None для текстовых полей (как _record_seen делает через
    # `card.address or None`) и для is_remote, если селектор не дал сигнала.
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="T",
        company="C",
        search_query="python",
        address=None,
        is_remote=None,
        experience=None,
        snippet_requirement=None,
        snippet_responsibility=None,
        side_job=None,
        no_resume=None,
    )
    row = h.list_vacancies_seen()[0]
    assert row["address"] == "Москва"
    assert row["experience"] == "between1And3"
    assert row["snippet_requirement"] == "req"
    assert row["snippet_responsibility"] == "resp"
    assert row["is_remote"] == 1
    assert row["side_job"] == 1
    assert row["no_resume"] == 1

    # Явное наблюдение «не удалённая» всё ещё должно обновить true.
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="T",
        company="C",
        search_query="python",
        is_remote=False,
    )
    assert h.list_vacancies_seen()[0]["is_remote"] == 0


def test_upsert_refreshes_optional_card_badges(tmp_path):
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="T",
        company="C",
        search_query="python",
        side_job=True,
        no_resume=True,
    )
    h.upsert_vacancy_seen(
        vacancy_id="123",
        title="T",
        company="C",
        search_query="python",
        side_job=False,
        no_resume=False,
    )
    row = h.list_vacancies_seen()[0]
    assert row["side_job"] == 0
    assert row["no_resume"] == 0


def test_upsert_keeps_all_known_fields_when_scrape_returns_empty_values(tmp_path):
    """Дрейф селектора не должен стирать best-known карточку (#532)."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="123",
        search_query="python",
        title="Backend",
        company="Yandex",
        salary_from=300000,
        salary_to=400000,
        salary_currency="RUB",
        employer_tier="top_tech",
        vacancy_text="Python and Docker",
        published_at="2026-08-23T00:00:00",
        address="Москва",
        is_remote=True,
        experience="between1And3",
        snippet_requirement="req",
        snippet_responsibility="resp",
    )
    h.upsert_vacancy_seen(
        vacancy_id="123",
        search_query="python",
        title="",
        company="",
        salary_from=None,
        salary_to=None,
        salary_currency=None,
        employer_tier=None,
        vacancy_text="",
        published_at=None,
        address="",
        is_remote=None,
        experience="",
        snippet_requirement="",
        snippet_responsibility="",
    )

    row = h.list_vacancies_seen()[0]
    assert row["title"] == "Backend"
    assert row["company"] == "Yandex"
    assert row["salary_from"] == 300000
    assert row["salary_to"] == 400000
    assert row["salary_currency"] == "RUB"
    assert row["employer_tier"] == "top_tech"
    assert row["vacancy_text"] == "Python and Docker"
    assert row["published_at"] == "2026-08-23T00:00:00"
    assert row["address"] == "Москва"
    assert row["is_remote"] == 1
    assert row["experience"] == "between1And3"
    assert row["snippet_requirement"] == "req"
    assert row["snippet_responsibility"] == "resp"


def test_upsert_replaces_salary_bounds_as_one_observation(tmp_path):
    """Частичная новая вилка не должна смешиваться со старой границей."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="123",
        search_query="python",
        salary_from=300000,
        salary_to=400000,
        salary_currency="RUB",
    )
    h.upsert_vacancy_seen(
        vacancy_id="123",
        search_query="python",
        salary_from=350000,
        salary_to=None,
        salary_currency="RUB",
    )

    row = h.list_vacancies_seen()[0]
    assert row["salary_from"] == 350000
    assert row["salary_to"] is None
    assert row["salary_currency"] == "RUB"


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
        search_query="python",
    )
    row = h.list_vacancies_seen()[0]
    assert row["salary_from"] is None
    assert row["salary_to"] is None
    assert row["salary_currency"] is None


def test_upsert_persists_vacancy_text_for_learning_report(tmp_path):
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen("1", "python", vacancy_text="Python and Docker")
    h.upsert_vacancy_seen("1", "backend", vacancy_text="Python and Docker")
    h.upsert_vacancy_seen("2", "python", vacancy_text="Python and SQL")
    assert sorted(h.list_vacancy_texts()) == ["Python and Docker", "Python and SQL"]


# --- employer_tier (#93) -----------------------------------------------------


def test_upsert_records_employer_tier(tmp_path):
    """#93: tier работодателя пишется в vacancies_seen для estimate_salary."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="Backend",
        company="Яндекс",
        salary_from=300000,
        salary_to=400000,
        salary_currency="RUB",
        search_query="python",
        employer_tier="top_tech",
    )
    row = h.list_vacancies_seen()[0]
    assert row["employer_tier"] == "top_tech"


def test_upsert_updates_employer_tier_on_rescrape(tmp_path):
    """#93: при повторном scrape tier обновляется (компания получила trusted/отзывы)."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(vacancy_id="1", title="T", company="C", search_query="python")
    assert h.list_vacancies_seen()[0]["employer_tier"] is None

    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="python",
        employer_tier="mid",
    )
    assert h.list_vacancies_seen()[0]["employer_tier"] == "mid"


def test_employer_tier_column_added_to_existing_db(tmp_path):
    """#93: колонка employer_tier добавляется в уже существующую БД (#51 паттерн).
    Моделируем «старую» БД: создаём таблицу БЕЗ employer_tier, открываем History —
    _ensure_column должен ALTER'ом довести схему без потери данных."""
    db = tmp_path / "h.db"
    # «Старая» схема (до #93): без employer_tier.
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE vacancies_seen ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, vacancy_id TEXT, title TEXT, "
        "company TEXT, salary_from INTEGER, salary_to INTEGER, "
        "salary_currency TEXT, search_query TEXT, "
        "first_seen_at TEXT, last_seen_at TEXT, "
        "UNIQUE (vacancy_id, search_query))"
    )
    conn.execute(
        "INSERT INTO vacancies_seen (vacancy_id, search_query, first_seen_at, last_seen_at) "
        "VALUES ('1', 'python', '2024-01-01', '2024-01-01')"
    )
    conn.commit()
    conn.close()

    # _init_schema должен идемпотентно добавить employer_tier, не потеряв строку.
    h = History(db)
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="python",
        employer_tier="unknown",
    )
    rows = h.list_vacancies_seen()
    assert len(rows) == 1  # старая строка не потеряна, не задвоилась
    assert rows[0]["employer_tier"] == "unknown"


# --- агрегаты рынка (медиана по сфере) ---------------------------------------


def test_market_salary_by_query_returns_median(tmp_path):
    """Главная цель #66: сравнение сфер по медианной ЗП.

    ``median_to`` — медиана верхних границ (потолок предложения). #125 добавил
    рядом ``median_from`` (медиана нижних границ), см. test_market_bounds.py:
    здесь у всех вакансий from == to, поэтому обе медианы совпадают.
    """
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
    """Вакансии без указанной ЗП учитываются в count, но в медиану не идут.

    #125: «без ЗП» здесь означает БЕЗ ОБЕИХ границ — вакансия с одной только
    нижней («от N») теперь считается данными и попадает в with_salary и в
    медиану «от» (см. test_market_bounds.py).
    """
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


# --- include_estimates (#93) -------------------------------------------------


def test_market_salary_default_excludes_estimates(tmp_path):
    """#93: по умолчанию include_estimates=False — вакансии без ЗП в медиану не
    идут, поле estimated=False (медиана реальная)."""
    h = History(tmp_path / "h.db")
    # top_tech: реальные ЗП 100..500 → медиана 300.
    for i, s in enumerate([100, 200, 300, 400, 500]):
        h.upsert_vacancy_seen(
            vacancy_id=f"t{i}",
            title="T",
            company="C",
            salary_from=s,
            salary_to=s,
            salary_currency="RUB",
            search_query="python",
            employer_tier="top_tech",
        )
    # вакансия без ЗП (не должна влиять на медиану без оценок).
    h.upsert_vacancy_seen(
        vacancy_id="x",
        title="T",
        company="C",
        search_query="python",
        employer_tier="top_tech",
    )
    rows = h.market_salary_by_query()
    row = rows[0]
    assert row["median_to"] == 300
    assert row["estimated"] is False
    assert row["count"] == 6
    assert row["with_salary"] == 5


def test_market_salary_with_estimates_fills_zero_median(tmp_path):
    """#93: include_estimates=True — сфера, где ВСЕ без ЗП, получает оценочную
    медиану по tier'ам (если по tier есть данные с ЗП в той же сфере)."""
    h = History(tmp_path / "h.db")
    # top_tech с ЗП: 100..500 → медиана 300 (источник оценки).
    for i, s in enumerate([100, 200, 300, 400, 500]):
        h.upsert_vacancy_seen(
            vacancy_id=f"t{i}",
            title="T",
            company="C",
            salary_from=s,
            salary_to=s,
            salary_currency="RUB",
            search_query="python",
            employer_tier="top_tech",
        )
    # вакансии БЕЗ ЗП того же tier'а — оценятся в 300.
    for i in range(3):
        h.upsert_vacancy_seen(
            vacancy_id=f"u{i}",
            title="T",
            company="C",
            search_query="python",
            employer_tier="top_tech",
        )

    plain = h.market_salary_by_query()[0]
    assert plain["with_salary"] == 5  # реальные

    est = h.market_salary_by_query(include_estimates=True)[0]
    assert est["estimated"] is True
    assert est["with_salary"] == 5  # coverage реальных не испорчен оценками
    # combined = 5 реальных (медиана 300) + 3 оценки (300) → медиана 300.
    assert est["median_to"] == 300


def test_market_salary_with_estimates_marks_estimated_flag(tmp_path):
    """#93: флаг estimated=True только когда в медиану реально вошли оценки
    (есть вакансии без ЗП и нашлась оценка). Без вакансий без ЗП — False."""
    h = History(tmp_path / "h.db")
    for i, s in enumerate([100, 200, 300, 400, 500]):
        h.upsert_vacancy_seen(
            vacancy_id=f"t{i}",
            title="T",
            company="C",
            salary_from=s,
            salary_to=s,
            salary_currency="RUB",
            search_query="python",
            employer_tier="top_tech",
        )
    est = h.market_salary_by_query(include_estimates=True)[0]
    # все вакансии с ЗП — оценки не понадобились, estimated=False.
    assert est["estimated"] is False


# --- свежесть вакансий: published_at (issue #429) ----------------------------


def test_upsert_stores_published_at(tmp_path):
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="python",
        published_at="2026-08-19T00:00:00",
    )
    row = h.list_vacancies_seen()[0]
    assert row["published_at"] == "2026-08-19T00:00:00"


def test_upsert_missing_published_at_preserves_previous_value(tmp_path):
    """Регрессия (code review PR #430): дата публикации неизменна, повторный
    scrape без даты (селектор не сработал/отсутствует) не должен затирать
    уже известное значение NULL'ом."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="python",
        published_at="2026-08-19T00:00:00",
    )
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="python",
        published_at=None,
    )
    row = h.list_vacancies_seen()[0]
    assert row["published_at"] == "2026-08-19T00:00:00"


def test_upsert_new_published_at_overwrites_previous_value(tmp_path):
    """Селектор снова сработал со свежим значением — новое значение побеждает."""
    h = History(tmp_path / "h.db")
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="python",
        published_at="2026-08-19T00:00:00",
    )
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="python",
        published_at="2026-08-20T00:00:00",
    )
    row = h.list_vacancies_seen()[0]
    assert row["published_at"] == "2026-08-20T00:00:00"


def test_vacancy_age_distribution_counts_each_vacancy_once(tmp_path):
    """Регрессия (code review PR #430): UNIQUE — (vacancy_id, search_query),
    одна и та же вакансия по нескольким запросам не должна считаться дважды."""
    h = History(tmp_path / "h.db")
    now = datetime(2026, 8, 20, 12, 0, 0)
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="python",
        published_at=now.isoformat(),
    )
    h.upsert_vacancy_seen(
        vacancy_id="1",
        title="T",
        company="C",
        search_query="django",
        published_at=now.isoformat(),
    )
    dist = h.vacancy_age_distribution(now=now)
    assert dist["<1 дня"] == 1
    assert sum(dist.values()) == 1


def test_vacancy_age_distribution_buckets_by_age(tmp_path):
    h = History(tmp_path / "h.db")
    now = datetime(2026, 8, 20, 12, 0, 0)
    ages_days = {"a": 0, "b": 3, "c": 15, "d": 45}
    for vacancy_id, days_ago in ages_days.items():
        h.upsert_vacancy_seen(
            vacancy_id=vacancy_id,
            title="T",
            company="C",
            search_query="python",
            published_at=(now - timedelta(days=days_ago)).isoformat(),
        )
    h.upsert_vacancy_seen(
        vacancy_id="e",
        title="T",
        company="C",
        search_query="python",
        published_at=None,
    )
    dist = h.vacancy_age_distribution(now=now)
    assert dist == {
        "<1 дня": 1,
        "1-7 дней": 1,
        "7-30 дней": 1,
        "30+ дней": 1,
        "неизвестно": 1,
    }

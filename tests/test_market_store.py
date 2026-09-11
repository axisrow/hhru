"""Интеграционные тесты общей рыночной базы data/market.db (#1106/#1109).

Рынок один на все аккаунты: конкурент-снимки и собранные вакансии живут в
общей базе, которую команды открывают независимо от ``--account``; личная
история (actions/лимиты/дедуп) остаётся в per-account history.db (доктрина
#706 — отдельный файл test_multi_account_isolation.py, без правок). С #1109
per-account history.db рыночных таблиц не создаёт вовсе: карточки пишутся
только сюда, а личная аналитика (funnel/adaptive) читает их параметром
``market=``.
"""

from __future__ import annotations

import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from hhru_bot.history import History
from hhru_bot.market_schema import MARKET_TABLES_DDL
from hhru_bot.market_store import DEFAULT_MARKET_PATH, MarketStore

pytestmark = pytest.mark.integration


def _make_legacy_history(db_path: Path) -> History:
    """History в форме ДО #1109: свежая схема + рыночные таблицы вручную.

    Легаси-базы, из которых ещё не мигрировали, имеют market-таблицы внутри
    history.db; после #1109 SCHEMA их не создаёт, поэтому для тестов миграции
    они досоздаются тем же DDL (MARKET_TABLES_DDL), каким их создавал старый
    бинарь.
    """
    history = History(db_path)
    with history._connect() as conn:
        conn.executescript(MARKET_TABLES_DDL)
    return history


def _seed_resume(store, resume_id: str, *, query: str = "Тестировщик") -> None:
    store.upsert_competitor_resume(
        {
            "resume_id": resume_id,
            "resume_url": f"https://hh.ru/resume/{resume_id}",
            "desired_role": "Инженер по тестированию",
            "salary_from": 100_000,
            "salary_to": 150_000,
            "salary_currency": "RUB",
            "experience_months": 24,
            "specializations": ["Тестировщик"],
            "employment_types": ["полная занятость"],
            "work_formats": ["удалённо"],
            "languages": ["Русский — Родной"],
            "education": ["Высшее образование"],
            "experience_summary": None,
            "achievements": None,
            "skills": [{"name": "pytest", "proficiency": None}],
            "content_hash": f"hash:{resume_id}",
        },
        search_query=query,
        search_rank=1,
        search_in="position",
    )


def test_collect_under_account_a_visible_to_report_under_account_b(tmp_path, monkeypatch, capsys):
    """Главный критерий #1106: collect под аккаунтом A виден report под B.

    Два полных аккаунта (разные history.db), общая market.db: run_report с
    args аккаунта B (его config/history) обязан увидеть резюме, записанные
    «collect'ом» аккаунта A, — потому что конкурент-команды открывают
    data/market.db, а не args.history.
    """
    market_path = tmp_path / "data" / "market.db"
    monkeypatch.setattr("hhru_bot.market_store.DEFAULT_MARKET_PATH", market_path)

    # Оба аккаунта существуют (файлы history.db создаются открытием).
    History(tmp_path / "data" / "accounts" / "alpha" / "history.db")
    beta_history = History(tmp_path / "data" / "accounts" / "beta" / "history.db")

    # «collect под аккаунтом A»: команда пишет в общую базу, не в args.history.
    MarketStore().upsert_vacancy_seen(
        vacancy_id="00001", search_query="Тестировщик", salary_from=100_000
    )
    _seed_resume(MarketStore(), "00002")

    # «report под аккаунтом B»: свежая команда с args аккаунта B.
    from hhru_bot.commands.competitors import run_report

    run_report(
        Namespace(
            text="Тестировщик",
            search_in=None,
            auth_mode=None,
            top=5,
            config=str(tmp_path / "data" / "accounts" / "beta" / "config.yaml"),
            history=str(beta_history.db_path),
        )
    )
    out = capsys.readouterr().out
    assert "Инженер по тестированию" in out

    # Личная история B рынок не получила: с #1109 в ней нет рыночных таблиц вовсе.
    with beta_history._connect() as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "competitor_resumes" not in tables
    assert "vacancies_seen" not in tables


def test_migration_copies_all_rows_once_and_never_deletes_sources(tmp_path, monkeypatch):
    """Миграция при первом открытии: все N строк едут, источник не трогается,
    маркер не даёт копировать повторно (дублей нет)."""
    data_dir = tmp_path / "data"
    market_path = data_dir / "market.db"
    monkeypatch.setattr("hhru_bot.market_store.DEFAULT_MARKET_PATH", market_path)

    # Корневая личная база основного аккаунта + один именованный аккаунт —
    # обе в легаси-форме (с рыночными таблицами внутри history.db).
    root = _make_legacy_history(data_dir / "history.db")
    _seed_resume(root, "00003")
    _seed_resume(root, "00004", query="Python")
    root.upsert_vacancy_seen(vacancy_id="00005", search_query="Тестировщик", salary_from=200_000)
    account = _make_legacy_history(data_dir / "accounts" / "testing" / "history.db")
    _seed_resume(account, "00006")

    market = MarketStore(market_path)
    assert len(market.list_competitor_resumes(None)) == 3
    assert len(market.list_vacancies_seen()) == 1

    # Повторное открытие — no-op: маркер в market_meta, дублей нет.
    market_again = MarketStore(market_path)
    assert len(market_again.list_competitor_resumes(None)) == 3

    # Источники не мутированы (копирование, не перенос): строки на месте.
    with root._connect() as conn:
        resumes = conn.execute("SELECT COUNT(*) AS n FROM competitor_resumes").fetchone()["n"]
        seen = conn.execute("SELECT COUNT(*) AS n FROM vacancies_seen").fetchone()["n"]
    assert (resumes, seen) == (2, 1)


def test_record_seen_writes_only_to_market_db(tmp_path, monkeypatch):
    """Единственная запись search (#1109): _record_seen с реальным MarketStore
    кладёт карточку ТОЛЬКО в общую market.db — в per-account history.db строки
    нет (таблицы там с #1109 не существует). Иначе регрессия «вернули двойную
    запись» или «забыл передать market=» пройдёт сюиту незамеченной."""
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.search import SalaryInfo, VacancyCard

    monkeypatch.setattr(
        "hhru_bot.market_store.DEFAULT_MARKET_PATH", tmp_path / "data" / "market.db"
    )
    history = History(tmp_path / "data" / "accounts" / "alpha" / "history.db")
    card = VacancyCard(
        vacancy_id="00007",
        title="Backend",
        company="Acme",
        url="https://hh.ru/vacancy/00007",
        salary=SalaryInfo(300000, 400000, "RUB", "raw"),
    )

    _record_seen([card], "python backend", market=MarketStore())

    market_rows = MarketStore(tmp_path / "data" / "market.db").list_vacancies_seen()
    assert [r["vacancy_id"] for r in market_rows] == ["00007"]
    assert market_rows[0]["salary_from"] == 300000
    assert market_rows[0]["search_query"] == "python backend"
    # Личная история карточку не получила: таблицы vacancies_seen в ней нет.
    with history._connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vacancies_seen'"
        ).fetchone()
    assert row is None


def test_record_seen_without_market_is_a_noop(tmp_path):
    """market=None (общая база не открылась) — запись молча пропускается,
    _record_seen не падает и ничего не пишет (поиску рынок не нужен)."""
    from hhru_bot.commands.search import _record_seen
    from hhru_bot.search import VacancyCard

    card = VacancyCard(
        vacancy_id="00008", title="Backend", company="Acme", url="https://hh.ru/vacancy/00008"
    )
    _record_seen([card], "python backend", market=None)  # не должно упасть


def test_funnel_and_adaptive_read_cards_from_market_db(tmp_path):
    """Ключевой критерий #1109: funnel_by_search_query/adaptive_report_facts
    работают на аккаунте, в чьей history.db НЕТ vacancies_seen, но карточки
    есть в market.db (атрибуция по запросу и vacancy-факты читаются из рынка);
    без market — мягкая деградация без исключений."""
    history = History(tmp_path / "data" / "accounts" / "alpha" / "history.db")
    market = MarketStore(tmp_path / "data" / "market.db")
    market.upsert_vacancy_seen(vacancy_id="00009", search_query="python", title="Backend")
    market.upsert_vacancy_seen(vacancy_id="00010", search_query="backend", title="DevOps")
    history.record_action("01234567", "00009", "apply", "success")
    history.record_action("01234567", "00010", "apply", "success")

    funnel = history.funnel_by_search_query(market=market)
    assert {row["search_query"]: row["sent"] for row in funnel} == {"python": 1, "backend": 1}
    # Отклик без собственного query, но с карточкой в market — атрибутирован.
    assert history.count_unattributed_applies(market=market) == 0

    facts = history.adaptive_report_facts(market=market)
    assert {row["vacancy_id"] for row in facts["vacancies"]} == {"00009", "00010"}
    assert len(facts["actions"]) == 2

    # Мягкая деградация: market=None — отклики уходят в NULL-группу, факты
    # вакансий пусты, исключений нет.
    degraded = history.funnel_by_search_query(market=None)
    assert [row["search_query"] for row in degraded] == [None]
    assert degraded[0]["sent"] == 2
    assert history.count_unattributed_applies(market=None) == 2
    assert history.adaptive_report_facts(market=None)["vacancies"] == []


def test_market_open_soft_fails_when_db_unavailable(tmp_path, monkeypatch, caplog):
    """open_market(): при невозможности открыть market.db возвращает None и
    логирует warning — команды продолжают работу без рынка (#1109)."""
    import logging

    # Путь-файл вместо каталога: mkdir(parents=True) упадёт, MarketStore не
    # откроется.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr("hhru_bot.market_store.DEFAULT_MARKET_PATH", blocked / "market.db")

    from hhru_bot.market_store import open_market

    with caplog.at_level(logging.WARNING, logger="hhru_bot.market"):
        assert open_market() is None
    assert any("market.db" in record.message for record in caplog.records)


def test_default_market_path_is_cwd_relative():
    """Дефолт — data/market.db относительно cwd, по образцу history (#1106)."""
    assert str(DEFAULT_MARKET_PATH) == str(Path("data") / "market.db")


def test_market_store_schema_has_no_history_only_tables(tmp_path):
    """market.db содержит рыночные таблицы + market_meta; personal-таблицы
    History в нём не создаются (разные SCHEMA, #1106)."""
    MarketStore(tmp_path / "market.db")
    with sqlite3.connect(tmp_path / "market.db") as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"vacancies_seen", "market_meta"} <= tables
    assert "actions" not in tables

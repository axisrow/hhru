"""Интеграционные тесты общей рыночной базы data/market.db (#1106).

Рынок один на все аккаунты: конкурент-снимки и собранные вакансии живут в
общей базе, которую команды открывают независимо от ``--account``; личная
история (actions/лимиты/дедуп) остаётся в per-account history.db (доктрина
#706 — отдельный файл test_multi_account_isolation.py, без правок).
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from hhru_bot.history import History
from hhru_bot.market_store import DEFAULT_MARKET_PATH, MarketStore

pytestmark = pytest.mark.integration


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

    alpha_history = History(tmp_path / "data" / "accounts" / "alpha" / "history.db")
    beta_history = History(tmp_path / "data" / "accounts" / "beta" / "history.db")
    alpha_history.start_competitor_collection("Тестировщик", 1)

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

    # Личная история B рынок не получила: конкурент-таблицы beta history пусты.
    with beta_history._connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM competitor_resumes").fetchone()["n"]
    assert n == 0


def test_migration_copies_all_rows_once_and_never_deletes_sources(tmp_path, monkeypatch):
    """Миграция при первом открытии: все N строк едут, источник не трогается,
    маркер не даёт копировать повторно (дублей нет)."""
    data_dir = tmp_path / "data"
    market_path = data_dir / "market.db"
    monkeypatch.setattr("hhru_bot.market_store.DEFAULT_MARKET_PATH", market_path)

    # Корневая личная база основного аккаунта + один именованный аккаунт.
    root = History(data_dir / "history.db")
    _seed_resume(root, "00003")
    _seed_resume(root, "00004", query="Python")
    root.upsert_vacancy_seen(vacancy_id="00005", search_query="Тестировщик", salary_from=200_000)
    account = History(data_dir / "accounts" / "testing" / "history.db")
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


def test_record_seen_dual_writes_to_market_db(tmp_path, monkeypatch):
    """Двойная запись search (#1106): _record_seen с реальным MarketStore
    кладёт карточку и в per-account history, и в общую market.db — иначе
    регрессия «забыл передать market=» пройдёт сюиту незамеченной."""
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

    _record_seen([card], "python backend", history, market=MarketStore())

    market_rows = MarketStore(tmp_path / "data" / "market.db").list_vacancies_seen()
    assert [r["vacancy_id"] for r in market_rows] == ["00007"]
    assert market_rows[0]["salary_from"] == 300000
    assert market_rows[0]["search_query"] == "python backend"
    # Личная история тоже записана (её читают joins аналитики).
    assert [r["vacancy_id"] for r in history.list_vacancies_seen()] == ["00007"]


def test_default_market_path_is_cwd_relative():
    """Дефолт — data/market.db относительно cwd, по образцу history (#1106)."""
    assert str(DEFAULT_MARKET_PATH) == str(Path("data") / "market.db")

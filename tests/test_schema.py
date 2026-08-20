"""Characterization-тесты схемы SQLite: SCHEMA создаёт все таблицы, идемпотентна.

Схема хранится одной константой ``history.SCHEMA`` и применяется через
``History._init_schema()`` как ``conn.executescript(SCHEMA)``. Системы миграций
в проекте нет (оверинжиниринг для маленького проекта): при сильных изменениях
базу пересоздают заново, ``CREATE TABLE IF NOT EXISTS`` делает повторное
``_init_schema`` идемпотентным.
"""

from __future__ import annotations

import sqlite3

import pytest

from hhru_bot.history import SCHEMA

pytestmark = pytest.mark.unit


def test_schema_creates_all_tables():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"actions", "responses", "manual_offers"} <= tables
        # служебных таблиц системы миграций быть не должно
        assert "schema_migrations" not in tables
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_resume_vacancy_apply" in indexes
        assert "idx_responses_status_changed_at" in indexes
    finally:
        conn.close()


def test_init_schema_is_idempotent_via_if_not_exists():
    # Двойной executescript на одной БД не падает и не дублирует объекты —
    # IF NOT EXISTS гарантия идемпотентности _init_schema.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"actions", "responses", "manual_offers"} <= tables
    finally:
        conn.close()


def test_unique_index_prevents_duplicate_success_apply():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v1','apply','success','','2026-01-01')"
        )
        # второй success-apply на ту же (resume_id, vacancy_id) должен нарушить
        # partial UNIQUE-индекс idx_resume_vacancy_apply
        try:
            conn.execute(
                "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
                "VALUES ('r1','v1','apply','success','','2026-01-02')"
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("ожидалась IntegrityError от UNIQUE-индекса")
    finally:
        conn.close()


def test_history_init_schema_idempotent_on_reopen(tmp_path):
    # Повторное открытие того же файла = повторный _init_schema на существующей
    # базе: не падает, данные на месте.
    from hhru_bot.history import History

    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "dry_run")
    assert h.has_applied("r1", "v1") is False

    h2 = History(tmp_path / "h.db")
    assert h2.has_applied("r1", "v1") is False

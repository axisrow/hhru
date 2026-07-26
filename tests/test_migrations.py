"""Characterization-тесты migrations: apply_migrations идемпотентен и применяет схему.

После переноса SCHEMA из history.py в migrations/001_actions.sql поведение History
не должно измениться, а миграции должны быть идемпотентны.
"""

from __future__ import annotations

import sqlite3

from hhru_bot.migrations import apply_migrations


def test_apply_migrations_creates_actions_table():
    conn = sqlite3.connect(":memory:")
    try:
        apply_migrations(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "actions" in tables
        assert "schema_migrations" in tables
    finally:
        conn.close()


def test_apply_migrations_is_idempotent():
    conn = sqlite3.connect(":memory:")
    try:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
        assert len(first) >= 1
        assert second == []
    finally:
        conn.close()


def test_unique_index_prevents_duplicate_success_apply():
    conn = sqlite3.connect(":memory:")
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v1','apply','success','','2026-01-01')"
        )
        # второй success-apply на ту же (resume_id, vacancy_id) должен нарушить UNIQUE-индекс
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


def test_history_works_after_migrations(tmp_path):
    from hhru_bot.history import History

    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "dry_run")
    assert h.has_applied("r1", "v1")
    # повторное открытие того же файла не падает и данные на месте
    h2 = History(tmp_path / "h.db")
    assert h2.has_applied("r1", "v1")

"""Схема SQLite + letter_variant (#17): идемпотентность DDL и ALTER-колонки.

Миграций в проекте нет (#50/#51): схема — константа SCHEMA в history.py,
CREATE TABLE IF NOT EXISTS применяется _init_schema. CAVEAT #51: IF NOT EXISTS
не добавляет колонку в существующую таблицу, поэтому letter_variant (#17)
добавляется через ALTER TABLE ADD COLUMN под идемпотентной обёрткой
PRAGMA table_info. Эти тесты страхуют инварианты схемы.
"""

from __future__ import annotations

import sqlite3

from hhru_bot.history import SCHEMA, History


def test_schema_creates_actions_table():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "actions" in tables
    finally:
        conn.close()


def test_schema_is_idempotent():
    # IF NOT EXISTS: повторное executescript той же схемы не падает.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "actions" in tables
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


def test_history_creates_letter_variant_column(tmp_path):
    # #17: History._init_schema добавляет actions.letter_variant (ALTER под обёрткой).
    History(tmp_path / "h.db")
    conn = sqlite3.connect(tmp_path / "h.db")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(actions)")}
    finally:
        conn.close()
    assert "letter_variant" in cols


def test_letter_variant_added_idempotently_on_reopen(tmp_path):
    # CAVEAT #51: повторное открытие той же БД не падает на 'duplicate column'.
    History(tmp_path / "h.db")
    History(tmp_path / "h.db")  # второй History на тот же файл — не должно упасть
    conn = sqlite3.connect(tmp_path / "h.db")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(actions)")}
    finally:
        conn.close()
    assert "letter_variant" in cols


def test_record_action_persists_letter_variant(tmp_path):
    # ТДД-контракт #17: letter_variant пишется в историю.
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success", reason="success", letter_variant="ai")
    conn = sqlite3.connect(tmp_path / "h.db")
    try:
        row = conn.execute("SELECT letter_variant FROM actions WHERE action='apply'").fetchone()
    finally:
        conn.close()
    assert row[0] == "ai"


def test_record_action_letter_variant_defaults_to_none(tmp_path):
    # backward compatible: callers без letter_variant (bump и пр.) пишут NULL.
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v2", "bump", "success", reason="bumped")
    conn = sqlite3.connect(tmp_path / "h.db")
    try:
        row = conn.execute("SELECT letter_variant FROM actions WHERE action='bump'").fetchone()
    finally:
        conn.close()
    assert row[0] is None


def test_history_works_after_schema_creation(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "dry_run")
    assert h.has_applied("r1", "v1")
    # повторное открытие того же файла не падает и данные на месте
    h2 = History(tmp_path / "h.db")
    assert h2.has_applied("r1", "v1")

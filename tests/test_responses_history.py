"""Characterization-тесты мониторинга ответов работодателей (#12, Этап 2).

Покрывает history.upsert_response / history.new_responses_since и миграцию
012_responses.sql: UNIQUE (resume_id, vacancy_id) без дублей, переход статуса
read→invitation фиксируется как «обновление» (status_changed_at сдвигается),
повтор того же статуса — «unchanged» (статус-дата не двигается, только last_seen).
Без браузера — только SQLite.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from hhru_bot.history import History
from hhru_bot.migrations import apply_migrations

# --- миграция 012_responses.sql ---------------------------------------------


def test_migration_creates_responses_table():
    conn = sqlite3.connect(":memory:")
    try:
        apply_migrations(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "responses" in tables
    finally:
        conn.close()


def test_migration_idempotent_responses():
    conn = sqlite3.connect(":memory:")
    try:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
        assert any("012" in name for name in first)
        assert second == []
    finally:
        conn.close()


# --- upsert_response: insert / update / unchanged ---------------------------


def test_upsert_inserts_new_response(tmp_path):
    h = History(tmp_path / "h.db")
    outcome = h.upsert_response("r1", "v1", "Acme", "read", "/chat/1")
    assert outcome == "inserted"

    with h._connect() as conn:
        row = conn.execute(
            "SELECT resume_id, vacancy_id, employer, status, chat_url "
            "FROM responses WHERE resume_id='r1' AND vacancy_id='v1'"
        ).fetchone()
    assert row["status"] == "read"
    assert row["employer"] == "Acme"
    assert row["chat_url"] == "/chat/1"


def test_upsert_status_change_updates_status_and_changed_at(tmp_path):
    """Кейс ишью #12: read → invitation без дубля, status_changed_at двигается."""
    h = History(tmp_path / "h.db")
    h.upsert_response("r1", "v1", "Acme", "read", "/chat/1")

    # Зафиксируем status_changed_at после первого insert.
    with h._connect() as conn:
        first_changed = conn.execute(
            "SELECT status_changed_at FROM responses WHERE resume_id='r1' AND vacancy_id='v1'"
        ).fetchone()["status_changed_at"]

    # Эмулируем промежуток времени: двигаем status_changed_at в прошлое, чтобы
    # убедиться, что второй upsert перезапишет его свежей меткой при смене статуса.
    past = (datetime.now() - timedelta(hours=2)).isoformat()
    with h._connect() as conn:
        conn.execute(
            "UPDATE responses SET status_changed_at=? WHERE resume_id='r1' AND vacancy_id='v1'",
            (past,),
        )

    outcome = h.upsert_response("r1", "v1", "Acme Corp", "invitation", "/chat/1")
    assert outcome == "updated"

    with h._connect() as conn:
        row = conn.execute(
            "SELECT status, status_changed_at FROM responses WHERE resume_id='r1' AND vacancy_id='v1'"
        ).fetchone()
    assert row["status"] == "invitation"
    # status_changed_at сдвинулся с прошлого на свежее (now).
    assert row["status_changed_at"] > past
    assert row["status_changed_at"] >= first_changed


def test_upsert_same_status_is_unchanged_and_does_not_move_changed_at(tmp_path):
    """Повтор того же статуса — НЕ «новый ответ»: status_changed_at зафиксирован."""
    h = History(tmp_path / "h.db")
    h.upsert_response("r1", "v1", "Acme", "invitation", "/chat/1")
    with h._connect() as conn:
        first_changed = conn.execute(
            "SELECT status_changed_at FROM responses WHERE resume_id='r1' AND vacancy_id='v1'"
        ).fetchone()["status_changed_at"]

    outcome = h.upsert_response("r1", "v1", "Acme", "invitation", "/chat/1")
    assert outcome == "unchanged"

    with h._connect() as conn:
        row = conn.execute(
            "SELECT status_changed_at, last_seen_at FROM responses "
            "WHERE resume_id='r1' AND vacancy_id='v1'"
        ).fetchone()
    # status_changed_at НЕ двигается при unchanged — это и есть различие new vs seen.
    assert row["status_changed_at"] == first_changed
    # last_seen_at, напротив, освежается (каждый обход видел эту вакансию).
    assert row["last_seen_at"] >= first_changed


def test_upsert_unique_constraint_no_duplicate_rows(tmp_path):
    """UNIQUE (resume_id, vacancy_id) — повторный insert не плодит дубль-строки."""
    h = History(tmp_path / "h.db")
    h.upsert_response("r1", "v1", "Acme", "read", "/c")
    h.upsert_response("r1", "v1", "Acme", "invitation", "/c")
    h.upsert_response("r1", "v1", "Acme", "discard", "/c")

    with h._connect() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM responses WHERE resume_id='r1' AND vacancy_id='v1'"
        ).fetchone()["c"]
    assert cnt == 1  # одна строка на пару, upsert перезаписал статус


def test_upsert_keys_pairs_by_resume_and_vacancy(tmp_path):
    """Одна вакансия у двух резюме — две независимые строки (resume_id — часть ключа)."""
    h = History(tmp_path / "h.db")
    h.upsert_response("r1", "v1", "Acme", "read", "/c")
    h.upsert_response("r2", "v1", "Acme", "invitation", "/c")

    with h._connect() as conn:
        rows = {
            r["resume_id"]: r["status"]
            for r in conn.execute("SELECT resume_id, status FROM responses WHERE vacancy_id='v1'")
        }
    assert rows == {"r1": "read", "r2": "invitation"}


# --- new_responses_since ----------------------------------------------------


def test_new_responses_since_returns_only_changed_after(tmp_path):
    h = History(tmp_path / "h.db")
    now = datetime.now()

    # Свежий (только что изменился) — попадает.
    h.upsert_response("r1", "v1", "Acme", "invitation", "/c1")
    # Старый статус-переход (2 дня назад) — отсекается since=now.
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO responses (resume_id, vacancy_id, employer, status, chat_url, "
            "last_seen_at, status_changed_at, created_at) "
            "VALUES ('r1','v2','Old','discard','/c2', ?, ?, ?)",
            (
                (now - timedelta(days=2)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
            ),
        )

    fresh = h.new_responses_since(now - timedelta(hours=1))
    assert [r["vacancy_id"] for r in fresh] == ["v1"]


def test_new_responses_since_filters_by_resume(tmp_path):
    h = History(tmp_path / "h.db")
    since = datetime.now() - timedelta(hours=1)
    h.upsert_response("r1", "v1", "Acme", "invitation", "/c1")
    h.upsert_response("r2", "v9", "Beta", "discard", "/c9")

    only_r1 = h.new_responses_since(since, resume_id="r1")
    assert [r["vacancy_id"] for r in only_r1] == ["v1"]


def test_new_responses_since_includes_inserted_rows(tmp_path):
    """Впервые заведённая строка — это «новый ответ» (created_at==status_changed_at)."""
    h = History(tmp_path / "h.db")
    since = datetime.now() - timedelta(seconds=1)
    h.upsert_response("r1", "v1", "Acme", "read", "/c1")

    rows = h.new_responses_since(since)
    assert len(rows) == 1
    assert rows[0]["vacancy_id"] == "v1"
    assert rows[0]["status"] == "read"
    # Ключи, нужные выводу команды responses.
    assert {
        "resume_id",
        "vacancy_id",
        "employer",
        "status",
        "chat_url",
        "status_changed_at",
    } <= set(rows[0].keys())


def test_new_responses_since_ordered_desc(tmp_path):
    """Свежие (status_changed_at позже) — первыми."""
    h = History(tmp_path / "h.db")
    since = datetime.now() - timedelta(hours=1)
    h.upsert_response("r1", "v1", "A", "read", "/c1")
    # v2 — чуть позже по часам: эмулируем через прямой UPDATE в прошлое у v1.
    h.upsert_response("r1", "v2", "B", "invitation", "/c2")
    with h._connect() as conn:
        conn.execute(
            "UPDATE responses SET status_changed_at=? WHERE resume_id='r1' AND vacancy_id='v1'",
            ((datetime.now() - timedelta(minutes=30)).isoformat(),),
        )

    rows = h.new_responses_since(since)
    assert [r["vacancy_id"] for r in rows] == ["v2", "v1"]

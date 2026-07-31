"""Журнал отправленных ответов replies (#108, решение #55 вариант A4).

replies — append-only таблица наших ответов работодателям в переписках
negotiations. ОТДЕЛЬНО от responses (#12): responses перезаписывается каждым
scrape'ом fetch_responses и затёр бы факт нашей отправки. Тот же паттерн, что
manual_offers (#13) и skipped (#87): своя таблица в общем SCHEMA-блоке,
CREATE TABLE IF NOT EXISTS, БЕЗ миграций (#50).

ВАЖНО (контракт #55): replies — источник для аналитики и планирования, но НЕ
единственный источник правды об отправке. Живой чат подтверждает финально
(пользователь мог ответить вручную с телефона). Тесты фиксируют именно этот
контракт: has_replied отвечает про НАШУ запись, не про состояние чата.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from hhru_bot.history import SCHEMA, History

# --- SCHEMA: таблица replies ----------------------------------------------


def test_schema_creates_replies_table():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "replies" in tables
    finally:
        conn.close()


def test_schema_replies_idempotent():
    # IF NOT EXISTS: повторное executescript не падает, таблица одна.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA)
        rows = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='replies'"
        ).fetchone()
        assert rows[0] == 1
    finally:
        conn.close()


def test_schema_replies_created_at_index():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_replies_created_at" in indexes
    finally:
        conn.close()


def test_replies_unique_topic_marker_collides():
    # Та же (topic, inbound_marker) дважды → IntegrityError.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO replies (topic, inbound_marker, status, created_at) "
            "VALUES ('t1','m1','success','2026-01-01')"
        )
        try:
            conn.execute(
                "INSERT INTO replies (topic, inbound_marker, status, created_at) "
                "VALUES ('t1','m1','success','2026-01-02')"
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("ожидалась IntegrityError от UNIQUE(topic, inbound_marker)")
    finally:
        conn.close()


def test_replies_unique_allows_different_markers_in_one_topic():
    # Одна переписка, разные входящие — разные строки (диалог продолжается).
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO replies (topic, inbound_marker, status, created_at) "
            "VALUES ('t1','m1','success','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO replies (topic, inbound_marker, status, created_at) "
            "VALUES ('t1','m2','success','2026-01-02')"
        )
        rows = conn.execute("SELECT inbound_marker FROM replies WHERE topic='t1'").fetchall()
        assert {r[0] for r in rows} == {"m1", "m2"}
    finally:
        conn.close()


def test_replies_resume_id_not_in_key_account_scope():
    # Account-scope (как responses #12): resume_id опционален и НЕ в ключе —
    # одна и та же (topic, marker) с разными resume_id не даёт двух строк.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO replies (topic, inbound_marker, resume_id, status, created_at) "
            "VALUES ('t1','m1','r1','success','2026-01-01')"
        )
        try:
            conn.execute(
                "INSERT INTO replies (topic, inbound_marker, resume_id, status, created_at) "
                "VALUES ('t1','m1','r2','success','2026-01-02')"
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("resume_id не должен входить в ключ UNIQUE")
    finally:
        conn.close()


# --- record_reply / has_replied -------------------------------------------


def test_record_reply_and_has_replied(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    assert h.has_replied("t1", "m1")
    assert not h.has_replied("t1", "m2")
    assert not h.has_replied("t2", "m1")


def test_has_replied_empty_history(tmp_path):
    h = History(tmp_path / "h.db")
    assert not h.has_replied("t1", "m1")


def test_record_reply_idempotent_same_marker(tmp_path):
    # Повторная запись той же (topic, marker) — no-op по UNIQUE, не плодит строки.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    h.record_reply("t1", "m1", status="success")
    assert len(h.replies_since(datetime(2000, 1, 1))) == 1


def test_record_reply_idempotent_does_not_overwrite_first_row(tmp_path):
    # Append-only: первая запись остаётся, повтор не перезаписывает поля.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success", note="первый")
    h.record_reply("t1", "m1", status="failed", note="второй")
    rows = h.replies_since(datetime(2000, 1, 1))
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["note"] == "первый"


def test_record_reply_different_markers_same_topic_distinct(tmp_path):
    # Разные входящие в одном чате — независимые ответы.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    h.record_reply("t1", "m2", status="success")
    assert h.has_replied("t1", "m1")
    assert h.has_replied("t1", "m2")
    assert len(h.replies_since(datetime(2000, 1, 1))) == 2


def test_record_reply_stores_optional_fields(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_reply(
        "t1",
        "m1",
        vacancy_id="v1",
        resume_id="r1",
        status="success",
        letter_variant="ai",
        note="ответ на приглашение",
    )
    row = h.replies_since(datetime(2000, 1, 1))[0]
    assert row["topic"] == "t1"
    assert row["inbound_marker"] == "m1"
    assert row["vacancy_id"] == "v1"
    assert row["resume_id"] == "r1"
    assert row["status"] == "success"
    assert row["letter_variant"] == "ai"
    assert row["note"] == "ответ на приглашение"


def test_record_reply_optional_fields_default_to_none(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    row = h.replies_since(datetime(2000, 1, 1))[0]
    assert row["vacancy_id"] is None
    assert row["resume_id"] is None
    assert row["letter_variant"] is None
    assert row["note"] is None


def test_has_replied_true_only_for_successful_send(tmp_path):
    # dry_run и failed — НЕ отправка: планирование не должно считать их
    # отвеченными, иначе --dry-run навсегда заблокировал бы боевой ответ
    # (в отличие от actions #3, где dry_run намеренно дедуплицирует отклик).
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="dry_run")
    h.record_reply("t2", "m2", status="failed")
    h.record_reply("t3", "m3", status="success")
    assert not h.has_replied("t1", "m1")
    assert not h.has_replied("t2", "m2")
    assert h.has_replied("t3", "m3")


def test_replies_are_recorded_for_all_statuses(tmp_path):
    # Append-only журнал: dry_run/failed пишутся (нужны для аналитики),
    # просто не считаются отправкой в has_replied.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="dry_run")
    h.record_reply("t2", "m2", status="failed")
    statuses = {r["status"] for r in h.replies_since(datetime(2000, 1, 1))}
    assert statuses == {"dry_run", "failed"}


def test_record_reply_surrogate_marker_is_opaque(tmp_path):
    # inbound_marker — непрозрачная строка: и реальный message_id (#107), и
    # суррогат «дата + хеш текста». Схема не завязана на конкретный вид.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "msg-9876543210", status="success")
    h.record_reply("t2", "2026-07-31:9f86d081884c", status="success")
    assert h.has_replied("t1", "msg-9876543210")
    assert h.has_replied("t2", "2026-07-31:9f86d081884c")


# --- replies_since ---------------------------------------------------------


def test_replies_since_filters_by_time(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    future = datetime.now() + timedelta(hours=1)
    past = datetime.now() - timedelta(hours=1)
    assert h.replies_since(past)
    assert h.replies_since(future) == []


def test_replies_since_empty_history(tmp_path):
    h = History(tmp_path / "h.db")
    assert h.replies_since(datetime(2000, 1, 1)) == []


def test_replies_since_newest_first(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    h.record_reply("t2", "m2", status="success")
    h.record_reply("t3", "m3", status="success")
    topics = [r["topic"] for r in h.replies_since(datetime(2000, 1, 1))]
    assert topics == ["t3", "t2", "t1"]


def test_replies_since_includes_created_at(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    row = h.replies_since(datetime(2000, 1, 1))[0]
    assert row["created_at"]
    datetime.fromisoformat(row["created_at"])  # ISO-формат, как в остальных таблицах


# --- регресс: существующие таблицы не тронуты ------------------------------


def test_existing_tables_still_created(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    assert h.has_applied("r1", "v1")
    h.record_skip("r1", "v2", "stopword_title")
    assert h.is_skipped("r1", "v2")

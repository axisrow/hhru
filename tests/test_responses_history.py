"""Characterization-тесты мониторинга ответов работодателей (#12, Этап 2).

Покрывает history.upsert_response / history.new_responses_since и миграцию
012_responses.sql (account-scope: UNIQUE по vacancy_id — страница
/applicant/negotiations общая, карточка не несёт признака резюме, поэтому ответ
НЕ клонируется под все resume_id): переход статуса read→invitation фиксируется
как «обновление» (status_changed_at сдвигается, прежний статус → last_status),
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


# --- upsert_response (account-scope, ключ по vacancy_id) --------------------


def test_upsert_inserts_new_response(tmp_path):
    h = History(tmp_path / "h.db")
    outcome = h.upsert_response("v1", "Acme", "read", "/chat/1")
    assert outcome == "inserted"

    with h._connect() as conn:
        row = conn.execute(
            "SELECT resume_id, vacancy_id, employer, status, last_status, chat_url "
            "FROM responses WHERE vacancy_id='v1'"
        ).fetchone()
    assert row["status"] == "read"
    # resume_id по умолчанию NULL (account-scope; атрибуция к резюме недоступна).
    assert row["resume_id"] is None
    # last_status при первом insert = NULL (смены статуса ещё не было).
    assert row["last_status"] is None
    assert row["employer"] == "Acme"
    assert row["chat_url"] == "/chat/1"


def test_upsert_status_change_updates_status_and_changed_at(tmp_path):
    """Кейс ишью #12: read → invitation без дубля, status_changed_at двигается."""
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "read", "/chat/1")

    # Зафиксируем status_changed_at после первого insert.
    with h._connect() as conn:
        first_changed = conn.execute(
            "SELECT status_changed_at FROM responses WHERE vacancy_id='v1'"
        ).fetchone()["status_changed_at"]

    # Эмулируем промежуток времени: двигаем status_changed_at в прошлое, чтобы
    # убедиться, что второй upsert перезапишет его свежей меткой при смене статуса.
    past = (datetime.now() - timedelta(hours=2)).isoformat()
    with h._connect() as conn:
        conn.execute(
            "UPDATE responses SET status_changed_at=? WHERE vacancy_id='v1'",
            (past,),
        )

    outcome = h.upsert_response("v1", "Acme Corp", "invitation", "/chat/1", response_date="сегодня")
    assert outcome == "updated"

    with h._connect() as conn:
        row = conn.execute(
            "SELECT status, last_status, response_date, status_changed_at "
            "FROM responses WHERE vacancy_id='v1'"
        ).fetchone()
    assert row["status"] == "invitation"
    # last_status = ПРЕДЫДУЩИЙ статус (read), скопированный в момент смены —
    # даёт «откуда → куда» (read→invitation) для дашборда «что нового».
    assert row["last_status"] == "read"
    assert row["response_date"] == "сегодня"
    # status_changed_at сдвинулся с прошлого на свежее (now).
    assert row["status_changed_at"] > past
    assert row["status_changed_at"] >= first_changed


def test_upsert_same_status_is_unchanged_and_does_not_move_changed_at(tmp_path):
    """Повтор того же статуса — НЕ «новый ответ»: status_changed_at зафиксирован."""
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "invitation", "/chat/1")
    with h._connect() as conn:
        first_changed = conn.execute(
            "SELECT status_changed_at FROM responses WHERE vacancy_id='v1'"
        ).fetchone()["status_changed_at"]

    outcome = h.upsert_response("v1", "Acme", "invitation", "/chat/1")
    assert outcome == "unchanged"

    with h._connect() as conn:
        row = conn.execute(
            "SELECT status_changed_at, last_seen_at FROM responses WHERE vacancy_id='v1'"
        ).fetchone()
    # status_changed_at НЕ двигается при unchanged — это и есть различие new vs seen.
    assert row["status_changed_at"] == first_changed
    # last_seen_at, напротив, освежается (каждый обход видел эту вакансию).
    assert row["last_seen_at"] >= first_changed


def test_upsert_unique_constraint_no_duplicate_rows(tmp_path):
    """UNIQUE (vacancy_id) — повторный insert не плодит дубль-строки."""
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "read", "/c")
    h.upsert_response("v1", "Acme", "invitation", "/c")
    h.upsert_response("v1", "Acme", "discard", "/c")

    with h._connect() as conn:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM responses WHERE vacancy_id='v1'").fetchone()[
            "c"
        ]
    assert cnt == 1  # одна строка на вакансию (account-scope), upsert перезаписал статус


def test_upsert_account_scope_no_cloning_across_resumes(tmp_path):
    """Регрессия Codex-critical: один ответ НЕ клонируется под разные resume_id.

    Команда раньше persist'ила каждую карточку под КАЖДЫЙ resume_id из конфига —
    это фабриковало данные (ответ резюме A приписывался бы и резюме B). Теперь
    upsert ключуется по vacancy_id (account-scope); resume_id опционален и НЕ
    входит в UNIQUE — одна карточка = одна строка независимо от числа резюме.
    """
    h = History(tmp_path / "h.db")
    # Имитируем «команду»: карточка persist'ится ОДИН РАЗ (без цикла по резюме).
    h.upsert_response("v1", "Acme", "invitation", "/c1")
    # Даже если будущая достоверная атрибуция передаст resume_id — дубля нет,
    # апдейтится та же строка.
    h.upsert_response("v1", "Acme", "discard", "/c1", resume_id="r1")

    with h._connect() as conn:
        rows = conn.execute(
            "SELECT resume_id, status FROM responses WHERE vacancy_id='v1'"
        ).fetchall()
    assert len(rows) == 1  # НЕ две строки (по одной на резюме) — одна (account-scope)
    assert rows[0]["status"] == "discard"
    assert rows[0]["resume_id"] == "r1"  # опциональная атрибуция сохранена при апдейте


def test_upsert_same_vacancy_different_topics_are_distinct_rows(tmp_path):
    """Регрессия Codex-critical round 2: одна вакансия → НЕСКОЛЬКО переписок.

    Ключ по vacancy_id затирал бы соседние переписки (разные topic = разные чаты,
    напр. отклик с разных резюме). Ключ (vacancy_id, topic) хранит каждую отдельно:
    статус и chat_url одной не затирают другую.
    """
    h = History(tmp_path / "h.db")
    # Две переписки по вакансии v1: topic=1 (invitation) и topic=2 (discard).
    h.upsert_response("v1", "Acme", "invitation", "/applicant/negotiations?topic=1", topic="1")
    h.upsert_response("v1", "Acme", "discard", "/applicant/negotiations?topic=2", topic="2")

    with h._connect() as conn:
        rows = {
            r["topic"]: (r["status"], r["chat_url"])
            for r in conn.execute("SELECT topic, status, chat_url FROM responses")
        }
    assert set(rows) == {"1", "2"}
    assert rows["1"] == ("invitation", "/applicant/negotiations?topic=1")
    assert rows["2"] == ("discard", "/applicant/negotiations?topic=2")

    # Повторный обход обновляет СВОЮ переписку, не трогая соседнюю.
    h.upsert_response("v1", "Acme", "response", "/applicant/negotiations?topic=1", topic="1")
    with h._connect() as conn:
        rows = {
            r["topic"]: r["status"] for r in conn.execute("SELECT topic, status FROM responses")
        }
    assert rows == {"1": "response", "2": "discard"}  # topic=2 не затёрт


def test_upsert_topic_null_groups_by_vacancy(tmp_path):
    """topic=None (ответ без чата): группируется по vacancy_id, не плодит дублей."""
    h = History(tmp_path / "h.db")
    h.upsert_response("v1", "Acme", "read", "/vacancy/v1")  # topic=None
    h.upsert_response("v1", "Acme", "discard", "/vacancy/v1")  # тот же vacancy, topic=None

    with h._connect() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM responses WHERE vacancy_id='v1' AND topic IS NULL"
        ).fetchone()["c"]
    assert cnt == 1  # одна строка (UNIQUE(vacancy_id, topic) с topic=NULL)


# --- new_responses_since ----------------------------------------------------


def test_new_responses_since_returns_only_changed_after(tmp_path):
    h = History(tmp_path / "h.db")
    now = datetime.now()

    # Свежий (только что изменился) — попадает.
    h.upsert_response("v1", "Acme", "invitation", "/c1")
    # Старый статус-переход (2 дня назад) — отсекается since=now.
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO responses (resume_id, vacancy_id, employer, status, chat_url, "
            "response_date, last_seen_at, status_changed_at, created_at) "
            "VALUES (NULL,'v2','Old','discard','/c2', NULL, ?, ?, ?)",
            (
                (now - timedelta(days=2)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
                (now - timedelta(days=2)).isoformat(),
            ),
        )

    fresh = h.new_responses_since(now - timedelta(hours=1))
    assert [r["vacancy_id"] for r in fresh] == ["v1"]


def test_new_responses_since_includes_inserted_rows(tmp_path):
    """Впервые заведённая строка — это «новый ответ» (created_at==status_changed_at)."""
    h = History(tmp_path / "h.db")
    since = datetime.now() - timedelta(seconds=1)
    h.upsert_response("v1", "Acme", "read", "/c1")

    rows = h.new_responses_since(since)
    assert len(rows) == 1
    assert rows[0]["vacancy_id"] == "v1"
    assert rows[0]["status"] == "read"
    # Ключи, нужные выводу команды responses.
    assert {
        "resume_id",
        "vacancy_id",
        "topic",
        "employer",
        "status",
        "last_status",
        "chat_url",
        "response_date",
        "status_changed_at",
    } <= set(rows[0].keys())


def test_new_responses_since_ordered_desc(tmp_path):
    """Свежие (status_changed_at позже) — первыми."""
    h = History(tmp_path / "h.db")
    since = datetime.now() - timedelta(hours=1)
    h.upsert_response("v1", "A", "read", "/c1")
    # v2 — чуть позже по часам: эмулируем через прямой UPDATE в прошлое у v1.
    h.upsert_response("v2", "B", "invitation", "/c2")
    with h._connect() as conn:
        conn.execute(
            "UPDATE responses SET status_changed_at=? WHERE vacancy_id='v1'",
            ((datetime.now() - timedelta(minutes=30)).isoformat(),),
        )

    rows = h.new_responses_since(since)
    assert [r["vacancy_id"] for r in rows] == ["v2", "v1"]

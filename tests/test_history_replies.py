"""Журнал отправленных ответов replies (#108, решение #55 вариант A4).

replies — append-only таблица наших ответов работодателям в переписках
negotiations. ОТДЕЛЬНО от responses (#12): responses перезаписывается каждым
scrape'ом fetch_responses и затёр бы факт нашей отправки. Тот же паттерн, что
manual_offers (#13) и skipped (#87): своя таблица в общем SCHEMA-блоке,
CREATE TABLE IF NOT EXISTS, БЕЗ миграций (#50).

Ключ — partial-UNIQUE(topic, inbound_marker) WHERE status='success' (тот же
приём, что idx_resume_vacancy_apply у actions): dry_run/failed ключ не занимают,
поэтому «сначала --dry-run, потом боевая отправка» не теряет факт отправки.

ВАЖНО (контракт #55): replies — источник для аналитики и планирования, но НЕ
единственный источник правды об отправке. Живой чат подтверждает финально
(пользователь мог ответить вручную с телефона). Тесты фиксируют именно этот
контракт: has_replied отвечает про НАШУ запись, не про состояние чата.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from hhru_bot.history import REPLY_STATUS_VALUES, SCHEMA, History

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


def test_replies_partial_unique_success_collides():
    # Та же (topic, inbound_marker) со статусом success дважды → IntegrityError.
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
            raise AssertionError("ожидалась IntegrityError от partial-UNIQUE replies")
    finally:
        conn.close()


def test_replies_partial_unique_allows_non_success_duplicates():
    # dry_run/failed НЕ занимают ключ: попытки копятся, и позднейший success
    # на ту же пару вставляется штатно.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        for status in ("dry_run", "dry_run", "failed", "success"):
            conn.execute(
                "INSERT INTO replies (topic, inbound_marker, status, created_at) "
                "VALUES ('t1','m1',?,'2026-01-01')",
                (status,),
            )
        rows = conn.execute("SELECT status FROM replies WHERE topic='t1'").fetchall()
        assert len(rows) == 4
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


def test_replies_resume_id_not_in_key():
    # resume_id опционален и НЕ в ключе: ключ — (topic, inbound_marker), один
    # ответ на одно входящее. Одна и та же (topic, marker) с разными resume_id
    # не даёт двух строк.
    # NB (#200): «не в ключе» ≠ «атрибуции не существует» — SSR отдаёт resumeId,
    # и record_reply_and_action его пишет. Ключ от этого не меняется: чат
    # принадлежит одному резюме, дублировать строку не по чему.
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
            raise AssertionError("resume_id не должен входить в ключ partial-UNIQUE")
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


def test_record_reply_success_not_overwritten_by_later_attempt(tmp_path):
    # Успешная отправка — терминальное состояние: повторный вызов (в т.ч. с
    # другим статусом) не затирает её и не плодит вторую success-строку.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success", note="первый")
    h.record_reply("t1", "m1", status="failed", note="второй")
    successes = [r for r in h.replies_since(datetime(2000, 1, 1)) if r["status"] == "success"]
    assert len(successes) == 1
    assert successes[0]["note"] == "первый"
    assert h.has_replied("t1", "m1")


def test_dry_run_then_success_is_recorded(tmp_path):
    # Штатный сценарий: сначала --dry-run, потом боевая отправка. Успех НЕ
    # должен потеряться из-за холостого прогона, иначе has_replied навсегда
    # остался бы False и журнал потерял бы факт отправки.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="dry_run")
    assert not h.has_replied("t1", "m1")
    h.record_reply("t1", "m1", status="success")
    assert h.has_replied("t1", "m1")


def test_failed_then_success_is_recorded(tmp_path):
    # Ретрай после сбоя отправки: успех должен записаться.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="failed")
    assert not h.has_replied("t1", "m1")
    h.record_reply("t1", "m1", status="success")
    assert h.has_replied("t1", "m1")


def test_failed_attempts_are_all_kept_for_analytics(tmp_path):
    # Неуспешные попытки не занимают ключ и копятся в журнале — append-only.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="failed", note="таймаут")
    h.record_reply("t1", "m1", status="failed", note="капча")
    rows = h.replies_since(datetime(2000, 1, 1))
    assert len(rows) == 2
    assert {r["note"] for r in rows} == {"таймаут", "капча"}
    assert not h.has_replied("t1", "m1")


def test_record_reply_idempotent_repeated_success(tmp_path):
    # Повторная успешная запись того же ответа — no-op по partial-UNIQUE.
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    h.record_reply("t1", "m1", status="success")
    assert len(h.replies_since(datetime(2000, 1, 1))) == 1


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


# --- валидация status ------------------------------------------------------


def test_record_reply_rejects_unknown_status(tmp_path):
    # Опечатка/синоним статуса не должна тихо проходить: строка легла бы в БД,
    # has_replied навсегда вернул бы False, и бот отправил бы работодателю
    # второе сообщение. Дешевле упасть на записи.
    h = History(tmp_path / "h.db")
    for bad in ("SUCCESS", "sent", "ok", "", "pending"):
        try:
            h.record_reply("t1", "m1", status=bad)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"ожидалась ValueError на status={bad!r}")
    assert h.replies_since(datetime(2000, 1, 1)) == []


def test_record_reply_accepts_known_statuses(tmp_path):
    h = History(tmp_path / "h.db")
    for i, status in enumerate(REPLY_STATUS_VALUES):
        h.record_reply(f"t{i}", f"m{i}", status=status)
    assert len(h.replies_since(datetime(2000, 1, 1))) == len(REPLY_STATUS_VALUES)


def test_reply_summary_excludes_dry_run_and_groups_variants(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success", letter_variant="template")
    h.record_reply("t2", "m2", status="success", letter_variant="ai")
    h.record_reply("t3", "m3", status="dry_run", letter_variant="ai")
    h.record_reply("t4", "m4", status="failed", letter_variant="template")

    summary = h.reply_summary(None, "all")
    assert summary["total"] == 2
    assert summary["period"] == {"success": 2, "failed": 1}
    assert summary["letter_variants"] == {"ai": 1, "template": 1}


def test_reply_summary_total_respects_period_like_summary_does(tmp_path):
    """total должен уважать --period так же, как соседний summary().total (#112 review).

    Старая (400 дней назад) и свежая успешные записи: period='today' должен
    видеть только свежую и в total, и в period.success — иначе «Всего
    отправлено» вводит в заблуждение рядом со «За период» на том же экране."""
    h = History(tmp_path / "h.db")
    old_ts = (datetime.now() - timedelta(days=400)).isoformat()
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO replies (topic, inbound_marker, status, created_at) "
            "VALUES (?, ?, 'success', ?)",
            ("t-old", "m-old", old_ts),
        )
    h.record_reply("t-new", "m-new", status="success")

    summary = h.reply_summary(None, "today")
    assert summary["total"] == 1
    assert summary["period"]["success"] == 1


def test_reply_status_values_match_actions_vocabulary():
    # Словарь тот же, что у actions (#55): без новых состояний.
    assert set(REPLY_STATUS_VALUES) == {"success", "failed", "dry_run"}


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
    # Явно РАЗНЫЕ created_at (через сырой INSERT): record_reply в тесном цикле
    # даёт одинаковый timestamp, и тогда проверялся бы только тайбрейк id DESC,
    # а не сам ключ сортировки created_at DESC.
    h = History(tmp_path / "h.db")
    with h._connect() as conn:
        for topic, created_at in (
            ("t1", "2026-01-01T10:00:00"),
            ("t2", "2026-01-03T10:00:00"),
            ("t3", "2026-01-02T10:00:00"),
        ):
            conn.execute(
                "INSERT INTO replies (topic, inbound_marker, status, created_at) "
                "VALUES (?, ?, 'success', ?)",
                (topic, f"m-{topic}", created_at),
            )
    topics = [r["topic"] for r in h.replies_since(datetime(2000, 1, 1))]
    assert topics == ["t2", "t3", "t1"]


def test_replies_since_tiebreak_newest_id_first(tmp_path):
    # Одинаковый created_at → тайбрейк по id DESC (свежая вставка первой).
    h = History(tmp_path / "h.db")
    with h._connect() as conn:
        for topic in ("t1", "t2"):
            conn.execute(
                "INSERT INTO replies (topic, inbound_marker, status, created_at) "
                "VALUES (?, ?, 'success', '2026-01-01T10:00:00')",
                (topic, f"m-{topic}"),
            )
    topics = [r["topic"] for r in h.replies_since(datetime(2000, 1, 1))]
    assert topics == ["t2", "t1"]


def test_replies_since_includes_created_at(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_reply("t1", "m1", status="success")
    row = h.replies_since(datetime(2000, 1, 1))[0]
    assert row["created_at"]
    datetime.fromisoformat(row["created_at"])  # ISO-формат, как в остальных таблицах


# --- атрибуция к резюме (#200) --------------------------------------------


def test_reply_summary_resume_filter_no_longer_returns_silent_zeros(tmp_path):
    """#200: stats --resume показывал нули, хотя ответы были.

    Причина была не «нечем заполнить», а «данные выбрасывались»: SSR отдаёт
    topicList[].resumeId (проверено 2026-08-16, 7/7 переписок), но
    record_reply_and_action его не принимал, и фильтр reply_summary по
    resume_id не матчил ни одной строки.
    """
    h = History(tmp_path / "h.db")
    h.record_reply_and_action("t1", "m1", vacancy_id="v1", resume_id="96223331", status="success")
    h.record_reply_and_action("t2", "m2", vacancy_id="v2", resume_id="11111111", status="success")

    mine = h.reply_summary("96223331", "all")
    assert mine["total"] == 1, "фильтр по резюме снова зануляет отчёт"
    assert mine["period"]["success"] == 1
    # account-wide продолжает видеть оба ответа
    assert h.reply_summary(None, "all")["total"] == 2


def test_record_reply_and_action_without_resume_id_keeps_account_wide_sentinel(tmp_path):
    """Дрейф SSR не должен ронять журналирование: NULL в replies, "" в actions.

    actions.resume_id объявлен NOT NULL, поэтому там сентинел — пустая строка
    (поведение до #200 сохранено для случая, когда hh.ru поле не отдал).
    """
    h = History(tmp_path / "h.db")
    h.record_reply_and_action("t1", "m1", vacancy_id="v1", status="success")

    with h._connect() as conn:
        assert conn.execute("SELECT resume_id FROM replies").fetchone()[0] is None
        assert (
            conn.execute("SELECT resume_id FROM actions WHERE action = 'reply'").fetchone()[0] == ""
        )


# --- регресс: существующие таблицы не тронуты ------------------------------


def test_existing_tables_still_created(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    assert h.has_applied("r1", "v1")
    h.record_skip("r1", "v2", "stopword_title")
    assert h.is_skipped("r1", "v2")

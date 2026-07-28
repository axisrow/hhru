"""Журнал отсева skipped (#87): таблица + методы record/is_skipped/clear.

skipped — append-only журнал причин отсева вакансий filter_candidates'ом.
Partial-UNIQUE(resume_id, vacancy_id, reason): один reason на пару, разные
reasons — разные строки (как actions/responses). БЕЗ миграций (#50): таблица
в общем SCHEMA-блоке, CREATE TABLE IF NOT EXISTS.

Двойная польза (из #87): stats по причинам отсева + КЭШ отсева — повторный
search не пересматривает уже отклонённые (экономия LLM/времени #74/#85).
"""

from __future__ import annotations

import sqlite3

from hhru_bot.history import SCHEMA, SKIP_REASONS, History

# --- SCHEMA: таблица skipped ----------------------------------------------


def test_schema_creates_skipped_table():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "skipped" in tables
    finally:
        conn.close()


def test_schema_skipped_idempotent():
    # IF NOT EXISTS: повторное executescript не падает, таблица одна.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA)
        rows = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='skipped'"
        ).fetchone()
        assert rows[0] == 1
    finally:
        conn.close()


def test_skipped_partial_unique_same_reason_collides():
    # Та же (resume_id, vacancy_id, reason) дважды → IntegrityError.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO skipped (resume_id, vacancy_id, reason, created_at) "
            "VALUES ('r1','v1','stopword_title','2026-01-01')"
        )
        try:
            conn.execute(
                "INSERT INTO skipped (resume_id, vacancy_id, reason, created_at) "
                "VALUES ('r1','v1','stopword_title','2026-01-02')"
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("ожидалась IntegrityError от partial-UNIQUE skipped")
    finally:
        conn.close()


def test_skipped_partial_unique_allows_different_reasons():
    # Разные reasons на одну (resume_id, vacancy_id) — разные строки.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO skipped (resume_id, vacancy_id, reason, created_at) "
            "VALUES ('r1','v1','stopword_title','2026-01-01')"
        )
        # Другая причина — должна пройти без ошибки.
        conn.execute(
            "INSERT INTO skipped (resume_id, vacancy_id, reason, created_at) "
            "VALUES ('r1','v1','already_applied','2026-01-02')"
        )
        rows = conn.execute(
            "SELECT reason FROM skipped WHERE resume_id='r1' AND vacancy_id='v1'"
        ).fetchall()
        assert {r[0] for r in rows} == {"stopword_title", "already_applied"}
    finally:
        conn.close()


# --- методы record_skip / is_skipped / clear_skipped ----------------------


def test_record_skip_and_is_skipped(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    assert h.is_skipped("r1", "v1")
    assert not h.is_skipped("r1", "v2")
    assert not h.is_skipped("r2", "v1")


def test_record_skip_idempotent_same_reason(tmp_path):
    # Повторная запись той же причины — no-op (partial-UNIQUE), не дублирует.
    h = History(tmp_path / "h.db")
    h.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    h.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    assert h.clear_skipped(SKIP_REASONS.STOPWORD_TITLE) == 1


def test_record_skip_different_reasons_distinct_rows(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    h.record_skip("r1", "v1", SKIP_REASONS.ALREADY_APPLIED)
    assert h.is_skipped("r1", "v1")  # любая причина
    assert h.is_skipped_for("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    assert h.is_skipped_for("r1", "v1", SKIP_REASONS.ALREADY_APPLIED)
    assert not h.is_skipped_for("r1", "v1", SKIP_REASONS.STOPWORD_EMPLOYER)


def test_is_skipped_empty_history(tmp_path):
    h = History(tmp_path / "h.db")
    assert not h.is_skipped("r1", "v1")
    assert not h.is_skipped_for("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)


def test_clear_skipped_all_returns_count(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    h.record_skip("r1", "v2", SKIP_REASONS.STOPWORD_EMPLOYER)
    h.record_skip("r2", "v1", SKIP_REASONS.ALREADY_APPLIED)
    deleted = h.clear_skipped()
    assert deleted == 3
    assert not h.is_skipped("r1", "v1")


def test_clear_skipped_by_reason(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    h.record_skip("r1", "v2", SKIP_REASONS.STOPWORD_TITLE)
    h.record_skip("r1", "v3", SKIP_REASONS.ALREADY_APPLIED)
    # Чистим только stopword_title: уйдут 2 строки, already_applied останется.
    deleted = h.clear_skipped(SKIP_REASONS.STOPWORD_TITLE)
    assert deleted == 2
    assert not h.is_skipped("r1", "v1")
    assert not h.is_skipped("r1", "v2")
    assert h.is_skipped("r1", "v3")  # already_applied выжил
    assert h.is_skipped_for("r1", "v3", SKIP_REASONS.ALREADY_APPLIED)


def test_clear_skipped_empty_returns_zero(tmp_path):
    h = History(tmp_path / "h.db")
    assert h.clear_skipped() == 0
    assert h.clear_skipped(SKIP_REASONS.STOPWORD_TITLE) == 0


def test_clear_skipped_unknown_reason_returns_zero(tmp_path):
    # Неизвестный reason (нет таких строк) → 0 удалённых, без ошибок.
    h = History(tmp_path / "h.db")
    h.record_skip("r1", "v1", SKIP_REASONS.STOPWORD_TITLE)
    assert h.clear_skipped(SKIP_REASONS.ALREADY_APPLIED) == 0
    assert h.is_skipped("r1", "v1")  # stopword_title не задет

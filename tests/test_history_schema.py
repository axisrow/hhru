"""Схема SQLite + letter_variant (#17): идемпотентность DDL и ALTER-колонки.

Миграций в проекте нет (#50/#51): схема — константа SCHEMA в history.py,
CREATE TABLE IF NOT EXISTS применяется _init_schema. CAVEAT #51: IF NOT EXISTS
не добавляет колонку в существующую таблицу, поэтому letter_variant (#17)
добавляется через ALTER TABLE ADD COLUMN под идемпотентной обёрткой
PRAGMA table_info. Эти тесты страхуют инварианты схемы.
"""

from __future__ import annotations

import sqlite3

import pytest

from hhru_bot.history import SCHEMA, SKIP_REASONS, History

pytestmark = pytest.mark.unit


def test_schema_creates_actions_table():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "actions" in tables
        assert "test_assignments" in tables
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


def test_history_creates_search_query_column(tmp_path):
    History(tmp_path / "h.db")
    conn = sqlite3.connect(tmp_path / "h.db")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(actions)")}
    finally:
        conn.close()
    assert "search_query" in cols


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


def test_record_action_persists_search_query(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success", search_query="python")
    conn = sqlite3.connect(tmp_path / "h.db")
    try:
        row = conn.execute("SELECT search_query FROM actions WHERE action='apply'").fetchone()
    finally:
        conn.close()
    assert row[0] == "python"


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
    assert h.has_applied("r1", "v1") is False
    # повторное открытие того же файла не падает и данные на месте
    h2 = History(tmp_path / "h.db")
    assert h2.has_applied("r1", "v1") is False


def test_history_migration_removes_skip_created_only_by_legacy_dry_run(tmp_path):
    db = tmp_path / "h.db"
    h = History(db)
    h.record_action("r1", "v1", "apply", "dry_run")
    h.record_skip("r1", "v1", SKIP_REASONS.ALREADY_APPLIED)

    History(db)

    assert not h.is_skipped("r1", "v1")


def test_history_migration_preserves_skip_backed_by_real_apply(tmp_path):
    db = tmp_path / "h.db"
    h = History(db)
    h.record_action("r1", "v1", "apply", "dry_run")
    h.record_action("r1", "v1", "apply", "success")
    h.record_skip("r1", "v1", SKIP_REASONS.ALREADY_APPLIED)

    History(db)

    assert h.is_skipped("r1", "v1")


def test_record_test_assignment_persists_and_reads_history(tmp_path):
    from datetime import datetime

    h = History(tmp_path / "h.db")
    detected_at = datetime(2026, 8, 16, 10, 0)
    h.record_test_assigned(
        None,
        "vacancy-2",
        "topic-1",
        "ЯМКЕТ",
        "https://yay-tech.ru",
        "Необходимо пройти небольшой тест: yay-tech.ru",
        detected_at=detected_at,
    )

    rows = h.test_assignments_since(datetime(2026, 8, 16, 9, 59))
    assert rows == [
        {
            "resume_id": None,
            "vacancy_id": "vacancy-2",
            "topic": "topic-1",
            "employer": "ЯМКЕТ",
            "test_url": "https://yay-tech.ru",
            "message_text": "Необходимо пройти небольшой тест: yay-tech.ru",
            "detected_at": "2026-08-16T10:00:00",
        }
    ]


def test_record_test_assigned_deduplicates_same_message(tmp_path):
    # Повторный обход responses --detect-external-tests перечитывает то же
    # сообщение чата снова (нет курсора по message_id) — без UNIQUE-индекса
    # каждый прогон вставлял бы дубль строки для того же факта.
    from datetime import datetime

    h = History(tmp_path / "h.db")
    h.record_test_assigned(
        None,
        "vacancy-2",
        "topic-1",
        "ЯМКЕТ",
        "https://yay-tech.ru",
        "Необходимо пройти небольшой тест: yay-tech.ru",
        detected_at=datetime(2026, 8, 16, 10, 0),
    )
    h.record_test_assigned(
        None,
        "vacancy-2",
        "topic-1",
        "ЯМКЕТ",
        "https://yay-tech.ru",
        "Необходимо пройти небольшой тест: yay-tech.ru",
        detected_at=datetime(2026, 8, 16, 11, 0),
    )

    rows = h.test_assignments_since(datetime(2026, 8, 16, 9, 59))
    assert len(rows) == 1


def test_record_test_assigned_keeps_same_text_from_different_chats(tmp_path):
    # Одна вакансия может дать несколько переписок (повторный отклик тем же
    # резюме через разные topic). Совпадающий шаблонный текст сообщения из
    # ДВУХ разных чатов — это два разных реальных события, не дубликат.
    from datetime import datetime

    h = History(tmp_path / "h.db")
    h.record_test_assigned(
        None,
        "vacancy-2",
        "topic-1",
        "ЯМКЕТ",
        "https://yay-tech.ru",
        "Необходимо пройти небольшой тест: yay-tech.ru",
        detected_at=datetime(2026, 8, 16, 10, 0),
    )
    h.record_test_assigned(
        None,
        "vacancy-2",
        "topic-2",
        "ЯМКЕТ",
        "https://yay-tech.ru",
        "Необходимо пройти небольшой тест: yay-tech.ru",
        detected_at=datetime(2026, 8, 16, 11, 0),
    )

    rows = h.test_assignments_since(datetime(2026, 8, 16, 9, 59))
    assert len(rows) == 2


def test_unique_index_prevents_duplicate_uncertain_apply():
    # #177: 'uncertain' дедуплицируется в has_applied(), значит UNIQUE-индекс
    # тоже обязан покрывать этот статус — иначе гонка/повтор может вставить
    # несколько uncertain-строк для одной (resume_id, vacancy_id).
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v1','apply','uncertain','','2026-01-01')"
        )
        try:
            conn.execute(
                "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
                "VALUES ('r1','v1','apply','uncertain','','2026-01-02')"
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("ожидалась IntegrityError от UNIQUE-индекса")
    finally:
        conn.close()


def test_unique_index_upgraded_on_existing_db_with_old_condition(tmp_path):
    # Существующая БД, созданная ДО #177, содержит индекс со старым условием
    # WHERE status IN ('success', 'dry_run') — без 'uncertain'. IF NOT EXISTS
    # не пересоздаст индекс с новым условием на уже существующей БД (тот же
    # caveat #51, что и для колонок) — _init_schema должен доводить его явно
    # (DROP INDEX IF EXISTS + CREATE), а не полагаться на IF NOT EXISTS.
    db_path = tmp_path / "h.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resume_id TEXT NOT NULL,
                vacancy_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX idx_resume_vacancy_apply
                ON actions(resume_id, vacancy_id)
                WHERE action = 'apply' AND status IN ('success', 'dry_run');
            """
        )
        conn.commit()
    finally:
        conn.close()

    History(db_path)  # открытие существующей "старой" БД должно довести индекс

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v1','apply','uncertain','','2026-01-01')"
        )
        try:
            conn.execute(
                "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
                "VALUES ('r1','v1','apply','uncertain','','2026-01-02')"
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("ожидалась IntegrityError после доводки индекса на старой БД")
    finally:
        conn.close()


def test_history_open_does_not_crash_on_preexisting_uncertain_duplicates(tmp_path, caplog):
    # #177 round 2 (Codex): 'uncertain' появился в PR #176 (уже в main) ДО того
    # как индекс стал его покрывать (этот PR). Значит на реальных установках
    # уже может существовать БД с дублями status='uncertain' для одной пары
    # (resume_id, vacancy_id), накопленными под старым индексом. Пересоздание
    # индекса с новым условием не должно ронять History() исключением —
    # это заблокировало бы вообще все команды бота до ручной починки.
    db_path = tmp_path / "h.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resume_id TEXT NOT NULL,
                vacancy_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX idx_resume_vacancy_apply
                ON actions(resume_id, vacancy_id)
                WHERE action = 'apply' AND status IN ('success', 'dry_run');
            """
        )
        # два uncertain-дубля для одной пары — легально под старым индексом
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v1','apply','uncertain','','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v1','apply','uncertain','','2026-01-02')"
        )
        conn.commit()
    finally:
        conn.close()

    import logging

    with caplog.at_level(logging.WARNING, logger="hhru_bot.history"):
        History(db_path)  # не должно бросить IntegrityError

    assert any("idx_resume_vacancy_apply" in r.message for r in caplog.records)


def test_index_not_rebuilt_when_already_current(tmp_path):
    # #177 round 2 (Codex): DROP+CREATE не должен выполняться, если текущее
    # определение индекса в sqlite_master уже совпадает с желаемым — иначе
    # каждый CLI-вызов делал бы лишнюю write-миграцию (лишние SQLite
    # write-locks). sqlite3.Connection — C-тип, execute() нельзя monkeypatch'ить
    # напрямую (immutable type), поэтому используем conn.set_trace_callback —
    # официальный API sqlite3 для перехвата исполняемых SQL-инструкций.
    from hhru_bot.history import _ensure_apply_index

    db_path = tmp_path / "h.db"
    History(db_path)  # первое открытие — индекс создаётся с нужным условием

    conn = sqlite3.connect(db_path)
    executed_sql: list[str] = []
    conn.set_trace_callback(executed_sql.append)
    try:
        _ensure_apply_index(conn)  # индекс уже актуален — должно быть no-op
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert not any("DROP INDEX" in sql for sql in executed_sql), executed_sql
    assert not any("CREATE UNIQUE INDEX idx_resume_vacancy_apply" in sql for sql in executed_sql), (
        executed_sql
    )


def test_ensure_apply_index_keeps_old_index_when_duplicates_found(tmp_path):
    # #177 round 3 (Codex): при обнаружении дублей функция раньше БЕЗУСЛОВНО
    # дропала старый индекс (DROP INDEX IF EXISTS) ДО проверки, а после —
    # не восстанавливала ничего, если дубли найдены. Это снимало DB-уровня
    # UNIQUE-защиту ПОЛНОСТЬЮ, включая для success пар, которые
    # раньше были защищены старым (более узким) индексом. Правильно: если
    # пересборку выполнить нельзя (есть дубли), старый индекс не трогать —
    # degraded (без 'uncertain' в условии), но не нулевая защита.
    from hhru_bot.history import _ensure_apply_index

    db_path = tmp_path / "h.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resume_id TEXT NOT NULL,
                vacancy_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX idx_resume_vacancy_apply
                ON actions(resume_id, vacancy_id)
                WHERE action = 'apply' AND status IN ('success', 'dry_run');
            """
        )
        # дубль uncertain — новый статус, старым индексом не покрывается
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v1','apply','uncertain','','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v1','apply','uncertain','','2026-01-02')"
        )
        conn.commit()

        _ensure_apply_index(conn)

        index_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_resume_vacancy_apply'"
        ).fetchone()
        assert index_row is not None, "старый индекс должен остаться, а не исчезнуть полностью"

        # старая (degraded) защита всё ещё работает для success
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r2','v2','apply','success','','2026-01-01')"
        )
        try:
            conn.execute(
                "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
                "VALUES ('r2','v2','apply','success','','2026-01-02')"
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover
            raise AssertionError("старый индекс должен был отсечь дубль success")
    finally:
        conn.close()

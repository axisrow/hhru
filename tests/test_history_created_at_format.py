"""Ручная reconciliation из CLAUDE.md должна реально сниматься кодом.

CLAUDE.md (раздел 6) предписывает единственный способ разрешить зависший
``uncertain`` — вставить ``success``-строку в ``actions`` напрямую через SQL с
``datetime('now')``. SQLite отдаёт для него ``'YYYY-MM-DD HH:MM:SS'`` в UTC, а
сам код пишет ``datetime.now().isoformat()`` — локальное время с разделителем
``'T'``. Расхождение ломает два независимых потребителя ``created_at``:

1. ``has_unresolved_uncertain`` сравнивает даты СТРОКОЙ (``created_at > ?``).
   ``' '`` (0x20) < ``'T'`` (0x54), поэтому при равной календарной дате
   документированная резолюция не «позже» uncertain-строки, и блокировка
   остаётся навсегда.
2. ``last_action_at``/``time_since_last`` парсят строку как naive datetime и
   вычитают из локального ``datetime.now()``: UTC-строка выглядит на величину
   смещения таймзоны старше, обходя кулдаун (``bump`` — 4 часа).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hhru_bot.history import History, _parse_recorded_at

pytestmark = pytest.mark.unit


def _insert_documented_resolution(db_path, resume_id: str, action: str) -> None:
    """Ровно тот INSERT, что предписан CLAUDE.md для ручной reconciliation."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at)
            VALUES (?, ?, ?, 'success', 'manual reconciliation: confirmed via hh.ru UI',
                    datetime('now'))
            """,
            (resume_id, resume_id, action),
        )
        conn.commit()
    finally:
        conn.close()


def test_documented_reconciliation_clears_unresolved_uncertain(tmp_path):
    """Резолюция по инструкции обязана снимать блокировку повторного запуска."""
    db = tmp_path / "history.db"
    history = History(str(db))
    history.record_action("account", "account", "create_resume", "uncertain", "click sent")
    assert history.has_unresolved_uncertain("account", "create_resume")

    _insert_documented_resolution(db, "account", "create_resume")

    assert not history.has_unresolved_uncertain("account", "create_resume")


def test_utc_written_row_is_not_seen_as_hours_old(tmp_path):
    """Действие, записанное «сейчас», не должно выглядеть старым для кулдауна."""
    db = tmp_path / "history.db"
    history = History(str(db))
    _insert_documented_resolution(db, "r1", "bump")

    elapsed = history.time_since_last("r1", "bump")

    assert elapsed is not None
    # Запись сделана только что; любой заметный сдвиг означает неверный разбор
    # таймзоны и обход 4-часового кулдауна bump.
    assert elapsed < timedelta(minutes=5), f"свежая запись выглядит как {elapsed} назад"


def test_uncertain_after_resolution_still_blocks(tmp_path):
    """Секундное округление резолюции не должно скрывать НОВЫЙ uncertain."""
    db = tmp_path / "history.db"
    history = History(str(db))
    history.record_action("account", "account", "create_resume", "uncertain", "first")
    _insert_documented_resolution(db, "account", "create_resume")
    assert not history.has_unresolved_uncertain("account", "create_resume")

    # Новая неподтверждённая попытка после резолюции обязана снова блокировать.
    history.record_action("account", "account", "create_resume", "uncertain", "second")

    assert history.has_unresolved_uncertain("account", "create_resume")


def test_isoformat_resolution_still_clears(tmp_path):
    """Штатный путь (success записан кодом) продолжает снимать блокировку."""
    db = tmp_path / "history.db"
    history = History(str(db))
    history.record_action("r1", "r1", "delete_resume", "uncertain", "click sent")
    history.record_action("r1", "r1", "delete_resume", "success", "confirmed")

    assert not history.has_unresolved_uncertain("r1", "delete_resume")


def test_later_resolution_wins_over_earlier_one(tmp_path):
    """Учитывается ПОСЛЕДНЯЯ резолюция, а не первая: MIN(id) вместо MAX(id)
    признал бы более раннюю success разрешающей и пропустил бы uncertain,
    записанный между двумя резолюциями."""
    db = tmp_path / "history.db"
    history = History(str(db))
    history.record_action("r2", "r2", "copy_resume", "success", "first ok")
    history.record_action("r2", "r2", "copy_resume", "uncertain", "click sent")
    history.record_action("r2", "r2", "copy_resume", "success", "second ok")
    assert not history.has_unresolved_uncertain("r2", "copy_resume")

    # Ещё один uncertain — уже после последней резолюции.
    history.record_action("r2", "r2", "copy_resume", "uncertain", "later click")

    assert history.has_unresolved_uncertain("r2", "copy_resume")


def test_resolution_row_itself_does_not_count_as_uncertain(tmp_path):
    """Граница строгая: uncertain засчитывается строго ПОЗЖЕ резолюции.

    При нестрогом сравнении (``id >= ?``) сама success-строка попадала бы в
    окно поиска uncertain-строк, а uncertain с тем же id не существует —
    поэтому страж проверяет, что разрешённый uncertain не «воскресает».
    """
    db = tmp_path / "history.db"
    history = History(str(db))
    uncertain_id = history.record_action("r3", "r3", "publish_resume", "uncertain", "click")
    success_id = history.record_action("r3", "r3", "publish_resume", "success", "confirmed")
    assert success_id == uncertain_id + 1

    assert not history.has_unresolved_uncertain("r3", "publish_resume")


def test_parse_recorded_at_normalizes_all_three_shapes():
    """Три формы ``created_at`` приводятся к одному локальному моменту.

    Код пишет ISO с 'T' (локальное время), ручная reconciliation — SQLite
    ``datetime('now')`` (UTC с пробелом), а строка со смещением может прийти из
    внешнего дампа/восстановления. Все три обязаны разбираться согласованно.
    """
    local = _parse_recorded_at("2026-08-29T16:03:10.556350")
    assert local == datetime(2026, 8, 29, 16, 3, 10, 556350)

    utc_space = _parse_recorded_at("2026-08-29 08:03:10")
    aware_offset = _parse_recorded_at("2026-08-29T08:03:10+00:00")
    # Обе описывают один момент UTC, значит и локальное представление одно.
    assert utc_space == aware_offset
    # Результат naive и в локальной зоне — его сравнивают с datetime.now().
    assert utc_space.tzinfo is None
    expected = datetime.fromisoformat("2026-08-29T08:03:10+00:00").astimezone()
    assert utc_space == expected.replace(tzinfo=None)


def test_documented_sql_snippet_matches_code_format(tmp_path):
    """SQL из CLAUDE.md обязан писать тот же формат, что и ``record_action``.

    Страж от повторения бага: раньше документация предписывала
    ``datetime('now')`` (UTC с пробелом), расходившийся с
    ``datetime.now().isoformat()`` кода.
    """
    claude_md = Path(__file__).resolve().parents[1] / "CLAUDE.md"
    snippet = "strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')"
    assert snippet in claude_md.read_text(encoding="utf-8"), (
        "CLAUDE.md должен рекомендовать локальное время с разделителем 'T'"
    )

    db = tmp_path / "history.db"
    history = History(str(db))
    history.record_action("r4", "r4", "bump", "success", "written by code")
    conn = sqlite3.connect(db)
    try:
        code_written = conn.execute("SELECT created_at FROM actions").fetchone()[0]
        doc_written = conn.execute(f"SELECT {snippet}").fetchone()[0]
    finally:
        conn.close()

    assert "T" in code_written and "T" in doc_written
    # Обе формы разбираются одинаково и дают локальное время.
    assert abs(_parse_recorded_at(doc_written) - _parse_recorded_at(code_written)) < timedelta(
        minutes=5
    )

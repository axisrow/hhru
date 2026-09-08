"""Очередь робот-анкет: резолв колонкой resolved_at (robot-reply, #1044-цепочка).

robot_questionnaires — append-only: факт «бот-анкета обнаружена» хранит аудит,
поэтому ответ помечается колонкой (resolved_at/answer), а не DELETE.
is_robot_questionnaire видит только незарезолвнутые строки — иначе чат после
ответа роботу скипался бы в reply-employers вечно.
"""

from __future__ import annotations

import sqlite3

import pytest

from hhru_bot.history import SCHEMA, History

pytestmark = pytest.mark.unit


def _mark(history: History, topic: str = "100000001") -> None:
    history.mark_robot_questionnaire(topic, vacancy_id="200000002", reason="robot_questionnaire")


# --- вердикты пользователя: приоритет над эвристиками детекта ---------------


def test_verdict_roundtrip_and_upsert(tmp_path):
    history = History(tmp_path / "h.db")
    assert history.robot_verdict("100000001") is None

    history.set_robot_verdict("100000001", verdict="robot")
    assert history.robot_verdict("100000001") == "robot"

    # Передумал — тот же topic перезаписывается, вторых строк не растёт.
    history.set_robot_verdict("100000001", verdict="human")
    assert history.robot_verdict("100000001") == "human"
    with history._connect() as conn:  # noqa: SLF001 - тест читает таблицу напрямую
        assert conn.execute("SELECT COUNT(*) FROM robot_verdicts").fetchone()[0] == 1


def test_invalid_verdict_is_fail_closed(tmp_path):
    history = History(tmp_path / "h.db")
    with pytest.raises(ValueError):
        history.set_robot_verdict("100000001", verdict="maybe")


def test_clear_verdict_is_idempotent(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_robot_verdict("100000001", verdict="robot")
    history.clear_robot_verdict("100000001")
    assert history.robot_verdict("100000001") is None
    history.clear_robot_verdict("100000001")
    assert history.robot_verdict("100000001") is None


def test_marked_topic_is_pending_until_resolved(tmp_path):
    history = History(tmp_path / "h.db")
    _mark(history)
    assert history.is_robot_questionnaire("100000001") is True

    history.resolve_robot_questionnaire("100000001", answer="Нет")

    assert history.is_robot_questionnaire("100000001") is False
    rows = history.list_robot_questionnaires()
    assert len(rows) == 1
    assert rows[0]["resolved_at"]
    assert rows[0]["answer"] == "Нет"


def test_double_resolve_overwrites_answer_for_multistep_questionnaires(tmp_path):
    """Живые анкеты многошаговые: робот задаёт следующий вопрос после нашего
    ответа — повторный resolve легален и перезаписывает ответ свежим."""
    history = History(tmp_path / "h.db")
    _mark(history)
    history.resolve_robot_questionnaire("100000001", answer="Да")
    history.resolve_robot_questionnaire("100000001", answer="Нет")
    row = history.robot_questionnaire_row("100000001")
    assert row["answer"] == "Нет"
    assert history.is_robot_questionnaire("100000001") is False


def test_resolve_of_unknown_topic_is_fail_closed(tmp_path):
    history = History(tmp_path / "h.db")
    with pytest.raises(ValueError):
        history.resolve_robot_questionnaire("no-such-topic", answer="Нет")


def test_legacy_db_without_columns_is_upgraded_in_place(tmp_path):
    """Старая БД (schema до robot-reply) без resolved_at/answer: _ensure_column
    добавляет колонки при открытии History, резолв работает без пересоздания."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    # Точная копия прежней (до robot-reply) схемы таблицы: колонок резолва нет.
    conn.executescript(SCHEMA)
    conn.execute("DROP TABLE robot_questionnaires")
    conn.execute(
        """CREATE TABLE robot_questionnaires (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               topic TEXT NOT NULL UNIQUE,
               vacancy_id TEXT,
               reason TEXT NOT NULL,
               detected_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO robot_questionnaires (topic, vacancy_id, reason, detected_at) "
        "VALUES ('100000001', '200000002', 'robot_questionnaire', '2026-09-08T12:00:00')"
    )
    conn.commit()
    conn.close()

    history = History(path)
    assert history.is_robot_questionnaire("100000001") is True
    history.resolve_robot_questionnaire("100000001", answer="Нет")
    assert history.is_robot_questionnaire("100000001") is False

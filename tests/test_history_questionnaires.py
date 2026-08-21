"""Persistence tests for append-only questionnaire research snapshots."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hhru_bot.history import History

pytestmark = pytest.mark.unit


def test_record_questionnaire_stores_questions_and_options_without_dedup(tmp_path):
    db_path = tmp_path / "history.db"
    history = History(db_path)
    questions = [
        {
            "body_index": 0,
            "text": "Ваш опыт?",
            "kind": "choice",
            "is_radio": True,
            "options": ["Да", "Нет"],
        }
    ]

    history.record_questionnaire(
        "marketing", "123", "https://hh.ru/vacancy/123", "Маркетолог", "Acme", questions
    )
    history.record_questionnaire(
        "marketing", "123", "https://hh.ru/vacancy/123", "Маркетолог", "Acme", questions
    )

    with sqlite3.connect(db_path) as conn:
        scans = conn.execute("SELECT vacancy_id FROM questionnaire_scans").fetchall()
        stored = conn.execute(
            "SELECT body_index, text, kind, is_radio, options_json "
            "FROM questionnaire_questions ORDER BY id"
        ).fetchall()

    assert scans == [("123",), ("123",)]
    assert stored[0][:4] == (0, "Ваш опыт?", "choice", 1)
    assert json.loads(stored[0][4]) == ["Да", "Нет"]
    assert len(stored) == 2


def test_questionnaire_schema_migrates_legacy_db_idempotently(tmp_path):
    """Existing #456 research databases receive the #473 audit columns once."""
    db_path = tmp_path / "history.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE questionnaire_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, resume_id TEXT NOT NULL,
                vacancy_id TEXT NOT NULL, vacancy_url TEXT NOT NULL, title TEXT NOT NULL,
                company TEXT NOT NULL, detected_at TEXT NOT NULL
            );
            CREATE TABLE questionnaire_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL, body_index INTEGER NOT NULL, text TEXT NOT NULL,
                kind TEXT NOT NULL, is_radio INTEGER NOT NULL, options_json TEXT NOT NULL
            );
            """
        )

    History(db_path)
    History(db_path)  # must not attempt duplicate ALTER TABLE ADD COLUMN

    with sqlite3.connect(db_path) as conn:
        scan_columns = {row[1] for row in conn.execute("PRAGMA table_info(questionnaire_scans)")}
        question_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(questionnaire_questions)")
        }

    assert "source" in scan_columns
    assert {"answer", "answer_source", "confidence", "filled", "run_id"} <= question_columns


def test_probe_questionnaire_keeps_new_audit_fields_empty(tmp_path):
    history = History(tmp_path / "history.db")
    history.record_questionnaire(
        "marketing", "123", "https://hh.ru/vacancy/123", "Маркетолог", "Acme", []
    )

    with history._connect() as conn:
        scan = conn.execute("SELECT source FROM questionnaire_scans").fetchone()
    assert scan["source"] == "probe"


def test_apply_questionnaire_audit_preserves_answer_fields_and_summary(tmp_path):
    history = History(tmp_path / "history.db")
    history.record_questionnaire(
        "marketing",
        "123",
        "https://hh.ru/vacancy/123",
        "Маркетолог",
        "Acme",
        [
            {
                "body_index": 0,
                "text": "Переезд?",
                "kind": "text",
                "is_radio": False,
                "options": [],
                "answer": "Да",
                "answer_source": "profile",
                "confidence": 1.0,
                "filled": True,
            },
            {
                "body_index": 1,
                "text": "Кейс?",
                "kind": "text",
                "is_radio": False,
                "options": [],
                "answer": "",
                "answer_source": "llm",
                "confidence": 0.2,
                "filled": False,
            },
        ],
        source="apply",
        run_id="run-473",
    )

    with history._connect() as conn:
        rows = conn.execute(
            "SELECT answer, answer_source, confidence, filled, run_id "
            "FROM questionnaire_questions ORDER BY body_index"
        ).fetchall()

    assert tuple(rows[0]) == ("Да", "profile", 1.0, 1, "run-473")
    assert tuple(rows[1]) == ("", "llm", 0.2, 0, "run-473")
    assert history.questionnaire_answer_summary() == {"profile": 1, "llm": 0, "unanswered": 1}

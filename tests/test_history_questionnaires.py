"""Persistence tests for append-only questionnaire research snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

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


def test_questionnaire_answer_summary_scoped_by_resume_and_period(tmp_path):
    """cycle-review #473: stats --resume/--period must not mix in other resumes.

    Regression test for a /review finding: questionnaire_answer_summary() had
    no filters even though it was printed inside the resume/period-scoped
    stats block, so it silently reported lifetime, all-resume totals.
    """
    history = History(tmp_path / "history.db")
    question = {
        "body_index": 0,
        "text": "Переезд?",
        "kind": "text",
        "is_radio": False,
        "options": [],
        "answer": "Да",
        "answer_source": "profile",
        "confidence": 1.0,
        "filled": True,
    }
    history.record_questionnaire(
        "marketing",
        "1",
        "https://hh.ru/vacancy/1",
        "Маркетолог",
        "Acme",
        [question],
        source="apply",
        run_id="run-a",
    )
    history.record_questionnaire(
        "backend",
        "2",
        "https://hh.ru/vacancy/2",
        "Разработчик",
        "Beta",
        [question],
        source="apply",
        run_id="run-b",
    )
    # Backdate the "marketing" scan outside a "today" window.
    with history._connect() as conn:
        stale = (datetime.now() - timedelta(days=2)).isoformat()
        conn.execute(
            "UPDATE questionnaire_scans SET detected_at = ? WHERE resume_id = 'marketing'",
            (stale,),
        )
        conn.commit()

    assert history.questionnaire_answer_summary(resume_id="backend") == {
        "profile": 1,
        "llm": 0,
        "unanswered": 0,
    }
    assert history.questionnaire_answer_summary(period="today") == {
        "profile": 1,
        "llm": 0,
        "unanswered": 0,
    }
    assert history.questionnaire_answer_summary() == {"profile": 2, "llm": 0, "unanswered": 0}


def test_list_questionnaire_audit_returns_apply_rows_newest_first(tmp_path):
    """#488: чтение аудита ответов, записанных живым apply."""
    history = History(tmp_path / "history.db")
    history.record_questionnaire(
        "marketing",
        "1",
        "https://hh.ru/vacancy/1",
        "Маркетолог",
        "Acme",
        [
            {
                "body_index": 0,
                "text": "Настоящим подтверждаю...",
                "kind": "text",
                "is_radio": False,
                "options": [],
                "answer": "Да",
                "answer_source": "profile",
                "confidence": 1.0,
                "filled": True,
                "template": "data_accuracy",
                "cluster": "compliance",
                "resolver_source": "static",
            }
        ],
        source="apply",
        run_id="run-1",
    )
    history.record_questionnaire(
        "marketing",
        "2",
        "https://hh.ru/vacancy/2",
        "Маркетолог",
        "Beta",
        [
            {
                "body_index": 0,
                "text": "Какие редакторы вы используете?",
                "kind": "text",
                "is_radio": False,
                "options": [],
                "answer": "",
                "answer_source": None,
                "confidence": 0.0,
                "filled": False,
                "template": None,
                "cluster": None,
                "resolver_source": None,
            }
        ],
        source="apply",
        run_id="run-2",
    )
    # probe-скан не должен попасть в аудит ответов.
    history.record_questionnaire(
        "marketing",
        "3",
        "https://hh.ru/vacancy/3",
        "Маркетолог",
        "Gamma",
        [
            {
                "body_index": 0,
                "text": "Вопрос из разведки?",
                "kind": "text",
                "is_radio": False,
                "options": [],
            }
        ],
        source="probe",
    )

    rows = history.list_questionnaire_audit()

    assert [row["vacancy_id"] for row in rows] == ["2", "1"]
    first = rows[0]
    assert first["text"] == "Какие редакторы вы используете?"
    assert first["filled"] == 0
    assert first["template"] is None
    second = rows[1]
    assert second["answer"] == "Да"
    assert second["answer_source"] == "profile"
    assert second["confidence"] == 1.0
    assert second["template"] == "data_accuracy"
    assert second["cluster"] == "compliance"
    assert second["resolver_source"] == "static"


def test_list_questionnaire_audit_filters_by_resume_template_and_confidence(tmp_path):
    history = History(tmp_path / "history.db")
    history.record_questionnaire(
        "marketing",
        "1",
        "https://hh.ru/vacancy/1",
        "Маркетолог",
        "Acme",
        [
            {
                "body_index": 0,
                "text": "Зарплата?",
                "kind": "text",
                "is_radio": False,
                "options": [],
                "answer": "от 200000",
                "answer_source": "profile",
                "confidence": 0.9,
                "filled": True,
                "template": "salary",
            }
        ],
        source="apply",
    )
    history.record_questionnaire(
        "backend",
        "2",
        "https://hh.ru/vacancy/2",
        "Разработчик",
        "Beta",
        [
            {
                "body_index": 0,
                "text": "Готовы к переезду?",
                "kind": "text",
                "is_radio": False,
                "options": [],
                "answer": "",
                "answer_source": "llm",
                "confidence": 0.3,
                "filled": False,
                "template": "relocation",
            }
        ],
        source="apply",
    )

    assert [row["vacancy_id"] for row in history.list_questionnaire_audit("backend")] == ["2"]
    assert [row["vacancy_id"] for row in history.list_questionnaire_audit(template="salary")] == [
        "1"
    ]
    low_conf = history.list_questionnaire_audit(low_confidence=True)
    assert [row["vacancy_id"] for row in low_conf] == ["2"]
    assert history.list_questionnaire_audit(limit=1) == history.list_questionnaire_audit()[:1]


def test_list_questionnaire_audit_low_confidence_excludes_null_confidence(tmp_path):
    """NULL confidence (обычный AIQuestionAnswerer) не считается низкой уверенностью."""
    history = History(tmp_path / "history.db")
    history.record_questionnaire(
        "marketing",
        "1",
        "https://hh.ru/vacancy/1",
        "Маркетолог",
        "Acme",
        [
            {
                "body_index": 0,
                "text": "Вопрос без оценки уверенности?",
                "kind": "text",
                "is_radio": False,
                "options": [],
                "answer": "ответ",
                "answer_source": "llm",
                "confidence": None,
                "filled": True,
            }
        ],
        source="apply",
    )

    assert history.list_questionnaire_audit(low_confidence=True) == []

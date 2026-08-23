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


def test_rekey_questionnaire_scans_migrates_slug_rows_to_resume_id(tmp_path):
    """#486: старые запуски probe --questionnaires-only ключевали
    questionnaire_scans слагом резюме из config.yaml вместо реального
    resume_id — list_scanned_questions()/questionnaire._scope() фильтруют по
    resume_id, поэтому такие строки были недостижимы через --resume."""
    history = History(tmp_path / "history.db")
    history.record_questionnaire(
        "python",
        "111",
        "https://hh.ru/vacancy/111",
        "Backend",
        "Acme",
        [
            {
                "body_index": 0,
                "text": "Готовы к переезду?",
                "kind": "text",
                "is_radio": False,
                "options": [],
            }
        ],
    )
    history.record_questionnaire(
        "b3236ebbff10f60ff30039ed1f6d5876645331",
        "222",
        "https://hh.ru/vacancy/222",
        "Backend",
        "Beta",
        [
            {
                "body_index": 0,
                "text": "Ваш опыт с Django?",
                "kind": "text",
                "is_radio": False,
                "options": [],
            }
        ],
    )

    rekeyed = history.rekey_questionnaire_scans(
        {"python": "b3236ebbff10f60ff30039ed1f6d5876645331", "unrelated_slug": "deadbeef"}
    )

    assert rekeyed == 1
    scoped = history.list_scanned_questions("b3236ebbff10f60ff30039ed1f6d5876645331")
    assert {row["vacancy_id"] for row in scoped} == {"111", "222"}
    assert history.list_scanned_questions("python") == []


def test_rekey_questionnaire_scans_is_idempotent(tmp_path):
    history = History(tmp_path / "history.db")
    questions = [
        {
            "body_index": 0,
            "text": "Готовы к переезду?",
            "kind": "text",
            "is_radio": False,
            "options": [],
        }
    ]
    history.record_questionnaire(
        "python", "111", "https://hh.ru/vacancy/111", "Backend", "Acme", questions
    )
    mapping = {"python": "hex123"}

    first = history.rekey_questionnaire_scans(mapping)
    second = history.rekey_questionnaire_scans(mapping)

    assert first == 1
    assert second == 0
    assert history.list_scanned_questions("hex123")[0]["vacancy_id"] == "111"


def test_rekey_questionnaire_scans_ignores_empty_mapping(tmp_path):
    history = History(tmp_path / "history.db")
    assert history.rekey_questionnaire_scans({}) == 0


def test_rekey_questionnaire_pending_migrates_slug_rows(tmp_path):
    """#486: очередь наследует слаг-ключ от скана, из которого её засеяли —
    та же миграция, что и у questionnaire_scans, но по своей таблице."""
    history = History(tmp_path / "history.db")
    history.record_questionnaire_pending(
        "python", [{"text": "Готовы к переезду?", "kind": "text", "reason": "нет шаблона"}]
    )

    rekeyed = history.rekey_questionnaire_pending({"python": "hex123"})

    assert rekeyed == 1
    assert len(history.list_questionnaire_pending("hex123")) == 1
    assert history.list_questionnaire_pending("python") == []


def test_rekey_questionnaire_pending_deletes_the_slug_row_on_conflict(tmp_path):
    """Один и тот же вопрос уже стоит в очереди под ОБОИМИ ключами — это
    происходит, если questionnaire learn без --resume уже пересеял вопрос под
    resume_id (после миграции scans), пока slug-строка ещё не убрана.
    UNIQUE(resume_id, question_key) не позволяет их слить UPDATE'ом; строка
    под resume_id новее и остаётся, осиротевшая slug-строка удаляется, а не
    висит недостижимой навсегда."""
    history = History(tmp_path / "history.db")
    question = [{"text": "Готовы к переезду?", "kind": "text", "reason": "нет шаблона"}]
    history.record_questionnaire_pending("python", question)
    history.record_questionnaire_pending("hex123", question)

    rekeyed = history.rekey_questionnaire_pending({"python": "hex123"})

    assert rekeyed == 1
    rows = history.list_questionnaire_pending("hex123")
    assert len(rows) == 1
    assert history.list_questionnaire_pending("python") == []


def test_rekey_questionnaire_pending_ignores_empty_mapping(tmp_path):
    history = History(tmp_path / "history.db")
    assert history.rekey_questionnaire_pending({}) == 0

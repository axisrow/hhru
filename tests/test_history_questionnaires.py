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


# --- аудит сохранённых ответов (#488) ---------------------------------------


def _audit_question(index: int, **overrides) -> dict:
    question = {
        "body_index": index,
        "text": f"Вопрос {index}?",
        "kind": "text",
        "is_radio": False,
        "options": [],
        "answer": f"Ответ {index}",
        "answer_source": "profile",
        "confidence": 1.0,
        "filled": True,
        "template": "salary",
        "cluster": "money",
        "resolver_source": "static",
    }
    question.update(overrides)
    return question


def _record_audit(history: History, resume_id: str, vacancy_id: str, questions: list[dict]) -> None:
    history.record_questionnaire(
        resume_id,
        vacancy_id,
        f"https://hh.ru/vacancy/{vacancy_id}",
        "Разработчик",
        "Acme",
        questions,
        source="apply",
        run_id=f"run-{vacancy_id}",
    )


def test_audit_returns_resolver_fields_and_skips_probe_scans(tmp_path):
    """Снимки probe вопросы собирают, но ни на что не отвечают — им тут не место."""
    history = History(tmp_path / "history.db")
    _record_audit(history, "backend", "1", [_audit_question(0)])
    history.record_questionnaire(
        "backend", "2", "https://hh.ru/vacancy/2", "Разработчик", "Acme", [_audit_question(0)]
    )

    rows = history.list_questionnaire_audit()

    assert len(rows) == 1
    assert rows[0]["vacancy_id"] == "1"
    assert rows[0]["answer"] == "Ответ 0"
    assert rows[0]["template"] == "salary"
    assert rows[0]["cluster"] == "money"
    assert rows[0]["resolver_source"] == "static"
    assert rows[0]["confidence"] == 1.0


def test_audit_keeps_every_answer_of_a_repeated_question(tmp_path):
    """В отличие от list_scanned_questions — дедупа по тексту тут быть не должно:
    один и тот же вопрос у двух работодателей мог получить разные ответы."""
    history = History(tmp_path / "history.db")
    _record_audit(history, "backend", "1", [_audit_question(0, text="Зарплата?", answer="200")])
    _record_audit(history, "backend", "2", [_audit_question(0, text="Зарплата?", answer="300")])

    rows = history.list_questionnaire_audit()

    assert [row["answer"] for row in rows] == ["200", "300"]


def test_audit_last_n_keeps_the_freshest_rows_in_chronological_order(tmp_path):
    """--last N — это последние N ответов, а не первые попавшиеся."""
    history = History(tmp_path / "history.db")
    for index in range(6):
        _record_audit(history, "backend", str(index), [_audit_question(0, answer=f"a{index}")])

    rows = history.list_questionnaire_audit(limit=2)

    assert [row["answer"] for row in rows] == ["a4", "a5"]


def test_audit_filters_by_resume_and_template(tmp_path):
    history = History(tmp_path / "history.db")
    _record_audit(history, "backend", "1", [_audit_question(0)])
    _record_audit(history, "marketing", "2", [_audit_question(0)])
    _record_audit(history, "backend", "3", [_audit_question(0, template="relocation")])

    assert [row["vacancy_id"] for row in history.list_questionnaire_audit("backend")] == ["1", "3"]
    assert [row["vacancy_id"] for row in history.list_questionnaire_audit(template="salary")] == [
        "1",
        "2",
    ]
    assert [
        row["vacancy_id"]
        for row in history.list_questionnaire_audit("backend", template="relocation")
    ] == ["3"]


def test_audit_low_confidence_selects_rows_the_resolver_refused_to_answer(tmp_path):
    """Признак — пустой answer (pipeline пишет его намеренно), а не filled:
    filled батчевый и у соседей неуверенного вопроса тоже равен нулю."""
    history = History(tmp_path / "history.db")
    _record_audit(
        history,
        "backend",
        "1",
        [
            _audit_question(0, answer="Точно", confidence=1.0, filled=False),
            _audit_question(1, answer="", answer_source="llm", confidence=0.2, filled=False),
        ],
    )

    rows = history.list_questionnaire_audit(low_confidence=True)

    assert [row["text"] for row in rows] == ["Вопрос 1?"]


def test_audit_shows_a_null_answer_but_low_confidence_does_not_select_it(tmp_path):
    """Граница предиката: колонка nullable, а ``answer = ''`` NULL не ловит.

    Инвариант «в apply-скане answer всегда заполнен» держит вызывающий, а не
    схема: так пишет pipeline, тогда как probe ключи аудита опускает — но идёт
    с source='probe' и отсекается первым условием. Строка со скана apply без
    колонок аудита всё равно видна в общем списке (как «нет ответа»), и только
    флагом не выбирается. Тест фиксирует это, чтобы поведение меняли осознанно.
    """
    history = History(tmp_path / "history.db")
    history.record_questionnaire(
        "backend",
        "1",
        "https://hh.ru/vacancy/1",
        "Разработчик",
        "Acme",
        [{"body_index": 0, "text": "Без аудита", "kind": "text", "is_radio": False, "options": []}],
        source="apply",
    )

    assert [row["answer"] for row in history.list_questionnaire_audit()] == [None]
    assert history.list_questionnaire_audit(low_confidence=True) == []

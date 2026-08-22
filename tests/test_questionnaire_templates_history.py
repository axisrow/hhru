"""Хранилище обучаемых шаблонов, примеров и очереди анкет (#482)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hhru_bot.history import SKIP_REASON_VALUES, SKIP_REASONS, History

pytestmark = pytest.mark.unit


def _pending_item(text: str, **overrides) -> dict:
    item = {"text": text, "kind": "text", "is_radio": False, "options": (), "reason": "нет шаблона"}
    item.update(overrides)
    return item


# --- шаблоны ----------------------------------------------------------------


def test_set_and_get_template_round_trip(tmp_path):
    history = History(tmp_path / "h.db")

    history.set_questionnaire_template("salary", mode="static", answer="от 250000")

    stored = history.get_questionnaire_templates()["salary"]
    assert stored["mode"] == "static"
    assert stored["answer"] == "от 250000"
    assert stored["cluster"] == "mixed"


def test_resume_override_wins_over_account_answer(tmp_path):
    """Критерий приёмки #482: resume override приоритетнее account-ответа."""
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="аккаунт")
    history.set_questionnaire_template("salary", mode="static", answer="резюме", resume_id="r1")

    assert history.get_questionnaire_templates("r1")["salary"]["answer"] == "резюме"
    assert history.get_questionnaire_templates()["salary"]["answer"] == "аккаунт"


def test_other_resume_does_not_see_foreign_override(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="аккаунт")
    history.set_questionnaire_template("salary", mode="static", answer="резюме", resume_id="r1")

    assert history.get_questionnaire_templates("r2")["salary"]["answer"] == "аккаунт"


def test_repeated_set_updates_instead_of_duplicating(tmp_path):
    db_path = tmp_path / "h.db"
    history = History(db_path)

    history.set_questionnaire_template("salary", mode="static", answer="первое")
    history.set_questionnaire_template("salary", mode="static", answer="второе")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT answer FROM questionnaire_templates").fetchall()
    assert rows == [("второе",)]


def test_account_scope_cannot_hold_duplicate_rows(tmp_path):
    """Пустая строка вместо NULL — иначе UNIQUE не дедуплицировал бы скоуп."""
    db_path = tmp_path / "h.db"
    History(db_path).set_questionnaire_template("salary", mode="static", answer="a")

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO questionnaire_templates "
                "(template, resume_id, cluster, mode, answer, created_at, updated_at) "
                "VALUES ('salary', '', 'mixed', 'static', 'b', 'now', 'now')"
            )


def test_schema_rejects_unknown_mode(tmp_path):
    db_path = tmp_path / "h.db"
    History(db_path)

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO questionnaire_templates "
                "(template, resume_id, cluster, mode, created_at, updated_at) "
                "VALUES ('t', '', 'mixed', 'magic', 'now', 'now')"
            )


def test_unset_removes_only_its_own_scope(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="аккаунт")
    history.set_questionnaire_template("salary", mode="static", answer="резюме", resume_id="r1")

    assert history.unset_questionnaire_template("salary", resume_id="r1") is True

    assert history.get_questionnaire_templates("r1")["salary"]["answer"] == "аккаунт"


def test_unset_missing_template_returns_false(tmp_path):
    assert History(tmp_path / "h.db").unset_questionnaire_template("нет такого") is False


def test_unset_also_drops_confirmed_examples_of_that_scope(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="a")
    history.confirm_questionnaire_example("salary", "Ваш желаемый доход?")

    history.unset_questionnaire_template("salary")

    assert history.get_confirmed_phrases() == {}


def test_list_templates_shows_both_scopes(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="static", answer="аккаунт")
    history.set_questionnaire_template("salary", mode="static", answer="резюме", resume_id="r1")

    rows = history.list_questionnaire_templates()

    assert {row["resume_id"] for row in rows} == {"", "r1"}


def test_templates_carry_confirmed_examples(tmp_path):
    history = History(tmp_path / "h.db")
    history.set_questionnaire_template("salary", mode="contextual", instruction="вилка")
    history.confirm_questionnaire_example("salary", "Ваш желаемый доход?")

    assert history.get_questionnaire_templates()["salary"]["examples"] == ["Ваш желаемый доход?"]


# --- подтверждённые формулировки -------------------------------------------


def test_confirmed_phrase_is_normalized(tmp_path):
    history = History(tmp_path / "h.db")

    history.confirm_questionnaire_example("salary", "  Ваши   Зарплатные Ожидания? ")

    assert history.get_confirmed_phrases() == {"ваши зарплатные ожидания?": "salary"}


def test_confirm_example_is_idempotent(tmp_path):
    db_path = tmp_path / "h.db"
    history = History(db_path)

    history.confirm_questionnaire_example("salary", "Доход?")
    history.confirm_questionnaire_example("salary", "Доход?")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM questionnaire_examples").fetchone()[0] == 1


def test_resume_phrase_overrides_account_phrase(tmp_path):
    history = History(tmp_path / "h.db")
    history.confirm_questionnaire_example("salary", "Доход?")
    history.confirm_questionnaire_example("custom", "Доход?", resume_id="r1")

    assert history.get_confirmed_phrases("r1")["доход?"] == "custom"
    assert history.get_confirmed_phrases()["доход?"] == "salary"


# --- очередь ----------------------------------------------------------------


def test_pending_round_trip(tmp_path):
    history = History(tmp_path / "h.db")

    assert history.record_questionnaire_pending(
        "r1",
        [_pending_item("Ваш опыт?", options=("Да", "Нет"), kind="choice", is_radio=True)],
        vacancy_id="v1",
        vacancy_url="https://hh.ru/vacancy/v1",
    )

    row = history.list_questionnaire_pending("r1")[0]
    assert row["question_text"] == "Ваш опыт?"
    assert row["question_key"] == "ваш опыт?"
    assert json.loads(row["options_json"]) == ["Да", "Нет"]
    assert row["is_radio"] == 1
    assert row["status"] == "pending"


def test_pending_deduplicates_same_question_across_vacancies(tmp_path):
    history = History(tmp_path / "h.db")

    history.record_questionnaire_pending("r1", [_pending_item("Ваш опыт?")], vacancy_id="v1")
    history.record_questionnaire_pending("r1", [_pending_item("Ваш опыт?")], vacancy_id="v2")

    rows = history.list_questionnaire_pending("r1")
    assert len(rows) == 1
    assert rows[0]["vacancy_id"] == "v2"


def test_pending_is_scoped_per_resume(tmp_path):
    history = History(tmp_path / "h.db")
    history.record_questionnaire_pending("r1", [_pending_item("Ваш опыт?")])
    history.record_questionnaire_pending("r2", [_pending_item("Ваш опыт?")])

    assert len(history.list_questionnaire_pending("r1")) == 1
    assert len(history.list_questionnaire_pending()) == 2


def test_empty_pending_batch_is_a_noop_success(tmp_path):
    assert History(tmp_path / "h.db").record_questionnaire_pending("r1", []) is True


def test_pending_limit_is_applied(tmp_path):
    history = History(tmp_path / "h.db")
    history.record_questionnaire_pending(
        "r1", [_pending_item("Первый"), _pending_item("Второй"), _pending_item("Третий")]
    )

    assert len(history.list_questionnaire_pending("r1", limit=2)) == 2


def test_resolve_pending_hides_it_from_the_default_listing(tmp_path):
    history = History(tmp_path / "h.db")
    history.record_questionnaire_pending("r1", [_pending_item("Ваш опыт?")])
    pending_id = history.list_questionnaire_pending("r1")[0]["id"]

    assert history.resolve_questionnaire_pending(pending_id) is True

    assert history.list_questionnaire_pending("r1") == []
    assert len(history.list_questionnaire_pending("r1", status="resolved")) == 1


def test_resolve_missing_pending_returns_false(tmp_path):
    assert History(tmp_path / "h.db").resolve_questionnaire_pending(404) is False


def test_record_pending_returns_false_on_sqlite_error(tmp_path):
    db_path = tmp_path / "h.db"
    history = History(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE questionnaire_pending")

    assert history.record_questionnaire_pending("r1", [_pending_item("Ваш опыт?")]) is False


# --- авто-разблокировка вакансий -------------------------------------------


def test_clear_pending_skips_removes_only_questionnaire_pending_reason(tmp_path):
    history = History(tmp_path / "h.db")
    history.record_skip("r1", "v1", SKIP_REASONS.QUESTIONNAIRE_PENDING)
    history.record_skip("r1", "v2", SKIP_REASONS.STOPWORD_TITLE)
    history.record_skip("r1", "v3", SKIP_REASONS.QUESTION_LOW_CONFIDENCE)

    assert history.clear_pending_skips() == 1

    assert history.is_skipped("r1", "v1") is False
    assert history.is_skipped("r1", "v2") is True
    assert history.is_skipped("r1", "v3") is True


def test_clear_pending_skips_can_be_scoped_to_one_resume(tmp_path):
    history = History(tmp_path / "h.db")
    history.record_skip("r1", "v1", SKIP_REASONS.QUESTIONNAIRE_PENDING)
    history.record_skip("r2", "v2", SKIP_REASONS.QUESTIONNAIRE_PENDING)

    assert history.clear_pending_skips("r1") == 1

    assert history.is_skipped("r2", "v2") is True


def test_new_skip_reason_is_exposed_to_cli_choices():
    assert SKIP_REASONS.QUESTIONNAIRE_PENDING in SKIP_REASON_VALUES


# --- миграции ---------------------------------------------------------------


def test_audit_columns_are_added_to_an_older_database(tmp_path):
    """_ensure_column доводит таблицу, созданную до #482, без пересоздания."""
    db_path = tmp_path / "h.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE questionnaire_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                body_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                kind TEXT NOT NULL,
                is_radio INTEGER NOT NULL,
                options_json TEXT NOT NULL
            )
            """
        )

    History(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(questionnaire_questions)")}
    assert {"template", "cluster", "resolver_source"} <= columns


def test_opening_the_same_database_twice_is_idempotent(tmp_path):
    db_path = tmp_path / "h.db"
    History(db_path).set_questionnaire_template("salary", mode="static", answer="a")

    assert History(db_path).get_questionnaire_templates()["salary"]["answer"] == "a"

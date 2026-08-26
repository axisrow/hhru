from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hhru_bot.history import CommandRunBusy, History

pytestmark = pytest.mark.unit


def _snapshot(*, role="AI Engineer", skills=None):
    return {
        "resume_id": "r1",
        "resume_url": "https://hh.ru/resume/r1",
        "desired_role": role,
        "salary_from": 100_000,
        "salary_to": 150_000,
        "salary_currency": "RUB",
        "experience_months": 48,
        "specializations": ["Разработчик"],
        "employment_types": ["полная занятость"],
        "work_formats": ["удалённо"],
        "languages": ["Русский — Родной"],
        "education": ["Высшее образование"],
        "experience_summary": None,
        "achievements": None,
        "skills": skills or [{"name": "Python", "proficiency": "Продвинутый уровень"}],
        "content_hash": f"hash:{role}:{skills}",
    }


def test_history_creates_competitor_tables(tmp_path):
    db = tmp_path / "history.db"
    History(db)
    with sqlite3.connect(db) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "competitor_resumes",
        "competitor_resume_skills",
        "competitor_resume_queries",
        "competitor_collection_runs",
    } <= tables


def test_upsert_current_snapshot_and_query_relations_are_atomic(tmp_path):
    history = History(tmp_path / "history.db")
    assert history.upsert_competitor_resume(_snapshot(), search_query="AI", search_rank=1) == "new"
    assert (
        history.upsert_competitor_resume(_snapshot(), search_query="LLM", search_rank=2)
        == "unchanged"
    )
    changed = _snapshot(role="AI Infrastructure Engineer", skills=[{"name": "Docker"}])
    assert history.upsert_competitor_resume(changed, search_query="AI", search_rank=3) == "updated"

    rows = history.list_competitor_resumes("AI")
    assert len(rows) == 1
    assert rows[0]["desired_role"] == "AI Infrastructure Engineer"
    assert rows[0]["skills"] == [{"name": "Docker", "proficiency": None}]
    assert len(history.list_competitor_resumes("LLM")) == 1


def test_upsert_preserves_all_skill_values_without_privacy_triggers(tmp_path):
    history = History(tmp_path / "history.db")
    skills = [
        {"name": "node.js"},
        {"name": "test@example.com"},
        {"name": "+7 999 123-45-67"},
        {"name": "https://example.com"},
    ]

    assert (
        history.upsert_competitor_resume(_snapshot(skills=skills), search_query="AI", search_rank=1)
        == "new"
    )

    assert [skill["name"] for skill in history.list_competitor_resumes("AI")[0]["skills"]] == [
        "+7 999 123-45-67",
        "https://example.com",
        "node.js",
        "test@example.com",
    ]
    with sqlite3.connect(tmp_path / "history.db") as conn:
        triggers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'competitor_resume_skills_no_contacts%'"
        ).fetchall()
    assert triggers == []


def test_existing_skill_privacy_schema_is_migrated_away(tmp_path):
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE competitor_resume_skills (
                resume_id TEXT NOT NULL,
                skill TEXT NOT NULL CHECK (instr(skill, '@') = 0),
                proficiency TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (resume_id, skill)
            );
            CREATE TRIGGER competitor_resume_skills_no_contacts
            BEFORE INSERT ON competitor_resume_skills
            WHEN instr(NEW.skill, '.') > 0
            BEGIN SELECT RAISE(ABORT, 'contact-like competitor skill'); END;
            INSERT INTO competitor_resume_skills
            VALUES ('old', 'Python', NULL, '2026-01-01', '2026-01-01');
        """)

    History(db)

    with sqlite3.connect(db) as conn:
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='competitor_resume_skills'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO competitor_resume_skills VALUES (?, ?, ?, ?, ?)",
            ("new", "test@example.com", None, "2026-01-02", "2026-01-02"),
        )
        rows = conn.execute(
            "SELECT resume_id,skill FROM competitor_resume_skills ORDER BY resume_id"
        ).fetchall()
    assert "CHECK" not in table_sql.upper()
    assert rows == [("new", "test@example.com"), ("old", "Python")]


def test_failed_write_rolls_back_previous_snapshot(tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_competitor_resume(_snapshot(), search_query="AI", search_rank=1)
    broken = _snapshot(role="broken", skills=[{}])
    with pytest.raises(KeyError):
        history.upsert_competitor_resume(broken, search_query="AI", search_rank=1)
    row = history.list_competitor_resumes("AI")[0]
    assert row["desired_role"] == "AI Engineer"
    assert row["skills"][0]["name"] == "Python"


def test_collection_run_status_and_limited_count(tmp_path):
    history = History(tmp_path / "history.db")
    run_id = history.start_competitor_collection("AI", 5)
    history.finish_competitor_collection(
        run_id,
        status="limited",
        pages_fetched=5,
        cards_seen=100,
        details_saved=100,
        details_failed=0,
    )
    assert history.count_limited_competitor_runs("AI") == 1
    assert history.count_limited_competitor_runs("other") == 0

    complete_id = history.start_competitor_collection("AI", 5)
    history.finish_competitor_collection(
        complete_id,
        status="complete",
        pages_fetched=2,
        cards_seen=30,
        details_saved=30,
        details_failed=0,
    )
    assert history.count_limited_competitor_runs("AI") == 0


def test_collection_checkpoint_persists_owner_heartbeat_and_resume_page(tmp_path):
    history = History(tmp_path / "history.db")
    run_id = history.start_competitor_collection("AI", 0)

    history.checkpoint_competitor_collection(
        run_id,
        pages_fetched=2,
        cards_seen=40,
        details_saved=31,
        details_failed=2,
        last_started_page=2,
        last_completed_page=1,
        resume_page=2,
        observed_page_size=20,
    )

    row = history.competitor_collection_runs()[0]
    assert row["owner_pid"] is not None
    assert row["heartbeat_at"] is not None
    assert row["last_started_page"] == 2
    assert row["last_completed_page"] == 1
    assert row["resume_page"] == 2
    assert row["observed_page_size"] == 20


def test_live_competitor_owner_is_not_recovered(tmp_path):
    history = History(tmp_path / "history.db")
    run_id = history.start_competitor_collection("AI", 0)

    with pytest.raises(CommandRunBusy):
        history.begin_competitor_collection("AI", 1)

    assert history.competitor_collection_runs()[0]["run_id"] == run_id
    assert history.competitor_collection_runs()[0]["status"] == "running"


def test_abrupt_process_exit_is_recovered_from_last_checkpoint(tmp_path):
    db = tmp_path / "history.db"
    run_file = tmp_path / "run-id"
    script = f"""
import os
from pathlib import Path
from hhru_bot.history import History
h = History({str(db)!r})
run_id = h.start_competitor_collection('AI', 0)
h.checkpoint_competitor_collection(
    run_id, pages_fetched=3, cards_seen=60, details_saved=47, details_failed=1,
    last_started_page=3, last_completed_page=2, resume_page=3, observed_page_size=20,
)
Path({str(run_file)!r}).write_text(run_id)
os._exit(9)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    child = subprocess.run([sys.executable, "-c", script], check=False, env=env)
    assert child.returncode == 9

    history = History(db)
    started = history.begin_competitor_collection("AI", 1, resume=True)
    rows = {row["run_id"]: row for row in history.competitor_collection_runs()}
    dead = rows[run_file.read_text()]
    assert dead["status"] == "partial"
    assert dead["pages_fetched"] == 3
    assert dead["cards_seen"] == 60
    assert "owner process exited" in dead["detail"]
    assert started["resume_page"] == 3
    assert started["resume_rank_offset"] == 60
    assert started["resume_observed_page_size"] == 20
    assert started["resumed_from_run_id"] == dead["run_id"]


def test_resume_uses_only_explicit_checkpoint_for_same_query(tmp_path):
    history = History(tmp_path / "history.db")
    first = history.start_competitor_collection("AI", 2)
    history.finish_competitor_collection(
        first,
        status="limited",
        pages_fetched=2,
        cards_seen=40,
        details_saved=40,
        details_failed=0,
        resume_page=2,
        last_started_page=1,
        last_completed_page=1,
        observed_page_size=20,
    )

    fresh = history.begin_competitor_collection("other", 1, resume=True)
    assert fresh["resume_page"] == 0
    history.finish_competitor_collection(
        fresh["run_id"],
        status="complete",
        pages_fetched=1,
        cards_seen=0,
        details_saved=0,
        details_failed=0,
    )
    resumed = history.begin_competitor_collection("AI", 1, resume=True)
    assert resumed["resume_page"] == 2
    assert resumed["resume_rank_offset"] == 40
    assert resumed["resumed_from_run_id"] == first


def test_resume_rank_offset_uses_exact_cards_seen_for_variable_page_sizes(tmp_path):
    history = History(tmp_path / "history.db")
    first = history.start_competitor_collection("AI", 2)
    history.finish_competitor_collection(
        first,
        status="limited",
        pages_fetched=2,
        cards_seen=120,
        details_saved=120,
        details_failed=0,
        resume_page=2,
        last_started_page=1,
        last_completed_page=1,
        observed_page_size=100,
    )

    resumed = history.begin_competitor_collection("AI", 1, resume=True)

    assert resumed["resume_page"] == 2
    assert resumed["resume_rank_offset"] == 120


def test_resume_rank_offset_excludes_in_progress_page_cards(tmp_path):
    """Issue #632/Codex review on PR #660: a heartbeat checkpoint taken
    mid-page (search results already parsed and counted into cards_seen,
    but not all of that page's details fetched yet) must not let the
    resumed run double-count that page's cards in resume_rank_offset.

    competitors.py sets `state["resume_page"] = page_num` *before* parsing
    the page (so an interruption resumes by re-parsing the same page), but
    `state["cards"]` is incremented right after parsing -- before all of
    that page's details are processed. A heartbeat firing in that window
    persists a checkpoint where `resume_page == last_started_page` (the
    same, not-yet-completed page) while `cards_seen` already includes that
    page's cards. Resuming must offset ranks only by cards from pages that
    were actually *completed* (up to last_completed_page), not by
    cards_seen verbatim -- otherwise the re-parsed page's cards get ranks
    shifted past where they belong, and repeated interruptions compound
    the drift.
    """
    history = History(tmp_path / "history.db")
    run_id = history.start_competitor_collection("AI", 0, requested_page_size=100)
    # Page 0 (100 cards) fully completed. Page 1's 100 cards were just
    # parsed (cards_seen jumps to 200) but its details are still being
    # fetched -- last_completed_page stays at 0, resume_page/
    # last_started_page point at the in-progress page 1. This is exactly
    # the mid-page heartbeat checkpoint shape (recorded here via
    # finish_competitor_collection so the run isn't left 'running' --
    # same checkpoint fields checkpoint_competitor_collection would have
    # persisted from a live heartbeat).
    history.finish_competitor_collection(
        run_id,
        status="partial",
        pages_fetched=2,
        cards_seen=200,
        details_saved=105,
        details_failed=0,
        last_started_page=1,
        last_completed_page=0,
        resume_page=1,
        observed_page_size=100,
        cards_seen_completed=100,
    )

    resumed = history.begin_competitor_collection("AI", 1, resume=True)

    assert resumed["resume_page"] == 1
    # Only page 0's 100 completed cards should offset ranks -- page 1 will
    # be re-parsed from scratch by the resumed run, so its own 100 cards
    # must not be double-counted into the offset.
    assert resumed["resume_rank_offset"] == 100


def test_resume_does_not_cross_requested_page_sizes(tmp_path):
    history = History(tmp_path / "history.db")
    smoke = history.start_competitor_collection("AI", 1, requested_page_size=20)
    history.finish_competitor_collection(
        smoke,
        status="limited",
        pages_fetched=1,
        cards_seen=20,
        details_saved=20,
        details_failed=0,
        resume_page=1,
        last_started_page=0,
        last_completed_page=0,
        observed_page_size=20,
    )

    production = history.begin_competitor_collection("AI", 1, requested_page_size=100, resume=True)

    assert production["resume_page"] == 0
    assert production["resumed_from_run_id"] is None
    assert production["resume_rank_offset"] == 0


def test_resume_does_not_cross_authentication_modes(tmp_path):
    history = History(tmp_path / "history.db")
    authenticated = history.start_competitor_collection("AI", 1, auth_mode="authenticated")
    history.finish_competitor_collection(
        authenticated,
        status="limited",
        pages_fetched=1,
        cards_seen=20,
        details_saved=20,
        details_failed=0,
        resume_page=1,
        last_started_page=0,
        last_completed_page=0,
        observed_page_size=20,
    )

    anonymous = history.begin_competitor_collection("AI", 1, auth_mode="anonymous", resume=True)

    assert anonymous["resume_page"] == 0
    assert anonymous["resumed_from_run_id"] is None
    assert anonymous["resume_rank_offset"] == 0
    assert history.competitor_collection_runs()[-1]["auth_mode"] == "anonymous"


def test_repeated_interruption_preserves_page_size_and_global_rank_offset(tmp_path):
    history = History(tmp_path / "history.db")
    first = history.start_competitor_collection("AI", 1)
    history.finish_competitor_collection(
        first,
        status="limited",
        pages_fetched=1,
        cards_seen=20,
        details_saved=20,
        details_failed=0,
        resume_page=1,
        last_started_page=0,
        last_completed_page=0,
        observed_page_size=20,
    )
    interrupted = history.begin_competitor_collection("AI", 1, resume=True)
    history.finish_competitor_collection(
        interrupted["run_id"],
        status="partial",
        pages_fetched=0,
        cards_seen=0,
        details_saved=0,
        details_failed=0,
        resume_page=1,
        last_started_page=1,
        last_completed_page=None,
        observed_page_size=interrupted["resume_observed_page_size"],
    )

    resumed_again = history.begin_competitor_collection("AI", 1, resume=True)
    assert resumed_again["resume_page"] == 1
    assert resumed_again["resume_observed_page_size"] == 20
    assert resumed_again["resume_rank_offset"] == 20


def test_completed_latest_run_prevents_resurrecting_older_checkpoint(tmp_path):
    history = History(tmp_path / "history.db")
    limited = history.start_competitor_collection("AI", 1)
    history.finish_competitor_collection(
        limited,
        status="limited",
        pages_fetched=1,
        cards_seen=20,
        details_saved=20,
        details_failed=0,
        resume_page=1,
        observed_page_size=20,
    )
    complete = history.start_competitor_collection("AI", 0)
    history.finish_competitor_collection(
        complete,
        status="complete",
        pages_fetched=50,
        cards_seen=1000,
        details_saved=1000,
        details_failed=0,
        resume_page=None,
        observed_page_size=20,
    )

    fresh = history.begin_competitor_collection("AI", 1, resume=True)
    assert fresh["resume_page"] == 0
    assert fresh["resumed_from_run_id"] is None
    assert fresh["resume_rank_offset"] == 0


def _snapshot_id(resume_id: str, *, role="AI Engineer"):
    snapshot = _snapshot(role=role)
    snapshot["resume_id"] = resume_id
    snapshot["resume_url"] = f"https://hh.ru/resume/{resume_id}"
    snapshot["content_hash"] = f"hash:{resume_id}:{role}"
    return snapshot


def test_report_scope_separates_search_in_populations(tmp_path):
    """#669: `full_text` по «AI» тянет дизайнеров с Adobe Illustrator (~81%
    мусора), `position` — только должность. Обе выборки живут под одним
    `search_query`, поэтому членство обязано ключеваться и по `search_in`:
    иначе отчёт молча смешает узкую популяцию с широкой."""
    history = History(tmp_path / "history.db")
    history.upsert_competitor_resume(
        _snapshot_id("designer", role="Графический дизайнер"),
        search_query="AI",
        search_rank=1,
        search_in="full_text",
    )
    history.upsert_competitor_resume(
        _snapshot_id("engineer"),
        search_query="AI",
        search_rank=1,
        search_in="position",
    )

    position = history.list_competitor_resumes("AI", search_in="position")
    full_text = history.list_competitor_resumes("AI", search_in="full_text")

    assert [row["resume_id"] for row in position] == ["engineer"]
    assert [row["resume_id"] for row in full_text] == ["designer"]
    # Без скоупа отчёт по-прежнему показывает всю базу.
    assert {row["resume_id"] for row in history.list_competitor_resumes("AI")} == {
        "designer",
        "engineer",
    }


def test_membership_rank_is_per_scope(tmp_path):
    """Одно резюме может попасть в обе выборки с разными рангами: узкий поиск
    ставит его выше. Общий ключ перезаписывал бы один ранг другим."""
    history = History(tmp_path / "history.db")
    history.upsert_competitor_resume(
        _snapshot_id("overlap"), search_query="AI", search_rank=317, search_in="full_text"
    )
    history.upsert_competitor_resume(
        _snapshot_id("overlap"), search_query="AI", search_rank=2, search_in="position"
    )

    with sqlite3.connect(tmp_path / "history.db") as conn:
        conn.row_factory = sqlite3.Row
        ranks = {
            row["search_in"]: row["search_rank"]
            for row in conn.execute(
                "SELECT search_in, search_rank FROM competitor_resume_queries WHERE resume_id='overlap'"
            )
        }
    assert ranks == {"full_text": 317, "position": 2}


def test_report_scope_separates_auth_mode_populations(tmp_path):
    """Та же ось у `auth_mode` (#663): анонимная выдача hh.ru урезана, а
    авторизованная полнее. Смешивать их в одном отчёте так же нельзя."""
    history = History(tmp_path / "history.db")
    history.upsert_competitor_resume(
        _snapshot_id("anon"), search_query="AI", search_rank=1, auth_mode="anonymous"
    )
    history.upsert_competitor_resume(
        _snapshot_id("authed"), search_query="AI", search_rank=1, auth_mode="authenticated"
    )

    anonymous = history.list_competitor_resumes("AI", auth_mode="anonymous")
    authenticated = history.list_competitor_resumes("AI", auth_mode="authenticated")

    assert [row["resume_id"] for row in anonymous] == ["anon"]
    assert [row["resume_id"] for row in authenticated] == ["authed"]


def test_legacy_membership_rows_are_scoped_as_full_text_anonymous(tmp_path):
    """Строки, записанные до #669, собирались при жёстком `pos=full_text` и
    единственном анонимном режиме — миграция обязана проставить именно их,
    иначе прежняя база выпадет из любого скоупа отчёта."""
    db = tmp_path / "history.db"
    History(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE competitor_resume_queries")
        conn.execute("""CREATE TABLE competitor_resume_queries (
            resume_id TEXT NOT NULL,
            search_query TEXT NOT NULL,
            search_rank INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (resume_id, search_query)
        )""")
        conn.execute(
            "INSERT INTO competitor_resume_queries VALUES ('legacy', 'AI', 7, '2026-08-01', '2026-08-01')"
        )

    history = History(db)
    history.upsert_competitor_resume(
        _snapshot_id("legacy"),
        search_query="AI",
        search_rank=7,
        search_in="full_text",
        auth_mode="unknown",
    )

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT search_in, auth_mode, search_rank FROM competitor_resume_queries"
                " WHERE resume_id='legacy'"
            )
        ]
    # `full_text` — факт (pos был жёстко зашит), а режим сессии в членстве не
    # хранился и был выбираемым, поэтому он неизвестен, а не анонимен.
    assert rows == [{"search_in": "full_text", "auth_mode": "unknown", "search_rank": 7}]


def test_resume_does_not_cross_search_scopes(tmp_path):
    """#669: `position` по «AI» даёт 619 резюме, `full_text` — ~5000. Номера
    страниц несопоставимы, поэтому чекпоинт одного режима не должен
    подхватываться другим (близнец теста по `auth_mode` выше)."""
    history = History(tmp_path / "history.db")
    legacy = history.start_competitor_collection("AI", 1)
    history.finish_competitor_collection(
        legacy,
        status="limited",
        pages_fetched=1,
        cards_seen=20,
        details_saved=20,
        details_failed=0,
        resume_page=3,
        last_started_page=2,
        last_completed_page=2,
        observed_page_size=20,
    )
    with sqlite3.connect(tmp_path / "history.db") as conn:
        conn.execute(
            "UPDATE competitor_collection_runs SET search_in=NULL WHERE run_id=?", (legacy,)
        )

    position = history.begin_competitor_collection("AI", 1, search_in="position", resume=True)
    assert position["resume_page"] == 0
    assert position["resumed_from_run_id"] is None
    history.finish_competitor_collection(
        position["run_id"],
        status="failed",
        pages_fetched=0,
        cards_seen=0,
        details_saved=0,
        details_failed=0,
        resume_page=None,
        last_started_page=None,
        last_completed_page=None,
        observed_page_size=None,
    )

    # Легаси-строка писалась при жёстком pos=full_text, поэтому именно
    # full_text обязан её подхватить.
    full_text = history.begin_competitor_collection("AI", 1, search_in="full_text", resume=True)
    assert full_text["resume_page"] == 3
    assert full_text["resumed_from_run_id"] == legacy


def test_interrupted_scope_migration_leaves_original_table_intact(tmp_path, monkeypatch):
    """#669: SQLite не держит DDL в транзакции по умолчанию, поэтому крах между
    RENAME и копирующим INSERT оставлял пустую новую таблицу рядом с legacy.
    Гвард видел в ней `search_in`, молча выходил — и всё членство исчезало
    из scoped-отчётов навсегда, без единой ошибки."""
    import hhru_bot.history as history_module

    db = tmp_path / "history.db"
    History(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE competitor_resume_queries")
        conn.execute("""CREATE TABLE competitor_resume_queries (
            resume_id TEXT NOT NULL,
            search_query TEXT NOT NULL,
            search_rank INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (resume_id, search_query)
        )""")
        conn.executemany(
            "INSERT INTO competitor_resume_queries VALUES (?, 'AI', ?, '2026-08-01', '2026-08-01')",
            [(f"r{i}", i) for i in range(50)],
        )

    original = history_module._migrate_competitor_query_scope_schema

    def crashing_migration(conn):
        # Падение ровно в момент копирования строк: RENAME/CREATE уже прошли.
        original(conn, _fail_after_create=True)

    monkeypatch.setattr(
        history_module, "_migrate_competitor_query_scope_schema", crashing_migration
    )
    with pytest.raises(sqlite3.OperationalError, match="simulated crash"):
        History(db)
    monkeypatch.undo()

    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'competitor_resume_queries%'"
            )
        }
        rows = conn.execute("SELECT COUNT(*) FROM competitor_resume_queries").fetchone()[0]
    # Откат обязан быть полным: ни осиротевшей legacy-таблицы, ни пустой новой.
    assert tables == {"competitor_resume_queries"}
    assert rows == 50

    # Повторное открытие доводит миграцию до конца — данные не потеряны.
    history = History(db)
    assert len(history.list_competitor_resumes("AI", search_in="full_text")) == 0  # снимков нет
    with sqlite3.connect(db) as conn:
        migrated = conn.execute(
            "SELECT COUNT(*) FROM competitor_resume_queries WHERE search_in='full_text'"
        ).fetchone()[0]
    assert migrated == 50


def test_legacy_membership_is_not_claimed_by_either_auth_mode(tmp_path):
    """#669: `--auth-mode authenticated` существовал до этой миграции, а в
    членстве режим не хранился. Пометить легаси-строки `anonymous` значило бы
    выдумать провенанс: authenticated-резюме исчезли бы из своего отчёта и
    загрязнили чужой. Неизвестное остаётся неизвестным."""
    db = tmp_path / "history.db"
    History(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE competitor_resume_queries")
        conn.execute("""CREATE TABLE competitor_resume_queries (
            resume_id TEXT NOT NULL,
            search_query TEXT NOT NULL,
            search_rank INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (resume_id, search_query)
        )""")
        conn.execute(
            "INSERT INTO competitor_resume_queries VALUES ('old', 'AI', 1, '2026-08-01', '2026-08-01')"
        )

    history = History(db)
    history.upsert_competitor_resume(
        _snapshot_id("old"),
        search_query="AI",
        search_rank=1,
        search_in="full_text",
        auth_mode="unknown",
    )

    # Ни один скоуп режима сессии не присваивает себе строку с неизвестным
    # происхождением — она видна только в общем отчёте.
    assert history.list_competitor_resumes("AI", auth_mode="anonymous") == []
    assert history.list_competitor_resumes("AI", auth_mode="authenticated") == []
    assert [row["resume_id"] for row in history.list_competitor_resumes("AI")] == ["old"]
    assert [
        row["resume_id"] for row in history.list_competitor_resumes("AI", search_in="full_text")
    ] == ["old"]


def test_legacy_membership_rekey_is_idempotent(tmp_path):
    """Сентинел вместо NULL: NULL в составном PRIMARY KEY не конфликтует сам с
    собой, поэтому повторная запись легаси-строки плодила бы дубликаты."""
    db = tmp_path / "history.db"
    History(db)
    history = History(db)
    for _ in range(2):
        history.upsert_competitor_resume(
            _snapshot_id("dup"), search_query="AI", search_rank=1, search_in="full_text"
        )

    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM competitor_resume_queries WHERE resume_id='dup'"
        ).fetchone()[0]
    assert count == 1


def test_interrupted_skills_migration_leaves_original_table_intact(tmp_path, monkeypatch):
    """Тот же незащищённый паттерн жил и в миграции навыков: обрыв копирования
    оставлял строки осиротевшими в legacy-таблице, а гвард (нет CHECK) больше
    не срабатывал — навыки исчезали молча."""
    import hhru_bot.history as history_module

    db = tmp_path / "history.db"
    History(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE competitor_resume_skills")
        conn.execute("""CREATE TABLE competitor_resume_skills (
            resume_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            proficiency TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            CHECK (skill NOT LIKE '%@%'),
            PRIMARY KEY (resume_id, skill)
        )""")
        conn.execute("INSERT INTO competitor_resume_skills VALUES ('r1','Python',NULL,'x','x')")

    original = history_module._migrate_competitor_skills_schema

    def crashing_migration(conn):
        # Обрываем ровно на копировании: RENAME и CREATE уже прошли, значит
        # откат обязан вернуть исходную таблицу вместе со строками.
        original(conn, _fail_after_create=True)

    monkeypatch.setattr(history_module, "_migrate_competitor_skills_schema", crashing_migration)
    with pytest.raises(sqlite3.OperationalError, match="simulated crash"):
        History(db)
    monkeypatch.undo()

    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name LIKE 'competitor_resume_skills%'"
            )
        }
        rows = conn.execute("SELECT COUNT(*) FROM competitor_resume_skills").fetchone()[0]
    assert tables == {"competitor_resume_skills"}
    assert rows == 1

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
    assert resumed["resumed_from_run_id"] == first

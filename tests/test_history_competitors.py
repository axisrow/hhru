from __future__ import annotations

import sqlite3

import pytest

from hhru_bot.history import History

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
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
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

"""Characterization-тесты агрегатов истории для команды stats (#11).

Покрывает новые методы History.summary / History.list_actions: пустой период
возвращает нули/пустой список, фильтрация по resume/периоду/статусу работает,
счётчики action корректны. Без браузера — только SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from hhru_bot.history import History

pytestmark = pytest.mark.unit


def _iso_days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


def test_summary_empty_returns_zeros(tmp_path):
    h = History(tmp_path / "h.db")
    s = h.summary(resume_id=None, period="all")
    assert s["apply"]["success"] == 0
    assert s["apply"]["dry_run"] == 0
    assert s["apply"]["failed"] == 0
    assert s["apply"]["uncertain"] == 0
    assert s["bump"]["success"] == 0
    assert s["bump"]["failed"] == 0
    assert s["total"] == 0


def test_summary_counts_by_action_and_status(tmp_path):
    h = History(tmp_path / "h.db")
    # 2 успешных отклика, 1 dry_run, 1 провал, 1 неопределённый; 1 успешный bump,
    # 1 проваленный bump, 1 неопределённый bump
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "success")
    h.record_action("r1", "v3", "apply", "dry_run")
    h.record_action("r1", "v4", "apply", "failed", "captcha")
    h.record_action("r1", "v8", "apply", "uncertain", "submit упал после клика")
    h.record_action("r1", "r1", "bump", "success")
    h.record_action("r1", "r1", "bump", "failed", "cooldown")
    h.record_action("r1", "r1", "bump", "uncertain", "клик упал после отправки")

    s = h.summary(resume_id="r1", period="all")
    assert s["apply"]["success"] == 2
    assert s["apply"]["dry_run"] == 1
    assert s["apply"]["failed"] == 1
    # #176: uncertain — отдельный бакет (не смешивается с failed), иначе total
    # сводки расходился бы с суммой статусных колонок stats
    assert s["apply"]["uncertain"] == 1
    assert s["bump"]["success"] == 1
    assert s["bump"]["failed"] == 1
    assert s["bump"]["uncertain"] == 1
    assert s["total"] == 8


def test_summary_filters_by_resume(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r2", "v9", "apply", "success")

    s = h.summary(resume_id="r1", period="all")
    assert s["apply"]["success"] == 1
    assert s["total"] == 1


def test_summary_period_excludes_old_rows(tmp_path):
    """period='week' отсекает действия старше 7 дней (по created_at)."""
    h = History(tmp_path / "h.db")
    # свежий (сегодня) — попадает
    h.record_action("r1", "v1", "apply", "success")
    # старый (30 дней назад) — отсекается периодом week
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v2','apply','success','', ?)",
            (_iso_days_ago(30),),
        )

    week = h.summary(resume_id=None, period="week")
    assert week["apply"]["success"] == 1

    allp = h.summary(resume_id=None, period="all")
    assert allp["apply"]["success"] == 2


def test_summary_period_month_boundary(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v2','apply','success','', ?)",
            (_iso_days_ago(40),),
        )

    assert h.summary(resume_id=None, period="month")["apply"]["success"] == 1
    assert h.summary(resume_id=None, period="all")["apply"]["success"] == 2


def test_list_actions_empty(tmp_path):
    h = History(tmp_path / "h.db")
    assert h.list_actions(resume_id=None, period="all", limit=10) == []


def test_list_actions_returns_dicts_ordered_desc(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "failed", "captcha")

    rows = h.list_actions(resume_id="r1", period="all", limit=10)
    assert len(rows) == 2
    # свежайшая запись первой (DESC по created_at, затем по id)
    assert rows[0]["vacancy_id"] == "v2"
    assert rows[0]["status"] == "failed"
    assert rows[1]["vacancy_id"] == "v1"
    # ключи, нужные форматтеру
    for r in rows:
        assert {"resume_id", "vacancy_id", "action", "status", "reason", "created_at"} <= set(
            r.keys()
        )


def test_list_actions_respects_limit(tmp_path):
    h = History(tmp_path / "h.db")
    for i in range(5):
        h.record_action("r1", f"v{i}", "apply", "success")

    rows = h.list_actions(resume_id="r1", period="all", limit=3)
    assert len(rows) == 3


def test_list_actions_filters_by_resume_and_period(tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r2", "v9", "apply", "success")  # чужое резюме
    with h._connect() as conn:
        conn.execute(
            "INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at) "
            "VALUES ('r1','v2','apply','success','', ?)",
            (_iso_days_ago(30),),
        )

    assert len(h.list_actions(resume_id="r1", period="all", limit=10)) == 2
    assert len(h.list_actions(resume_id="r1", period="week", limit=10)) == 1

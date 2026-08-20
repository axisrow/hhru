from __future__ import annotations

import json

import pytest

from hhru_bot.history import History
from hhru_bot.resume_views import parse_resume_view_history

pytestmark = pytest.mark.unit


def _html(state: dict) -> str:
    return '<template id="HH-Lux-InitialState">' + json.dumps(state) + "</template>"


def test_parse_resume_view_history_reads_ssr_and_limit():
    html = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": [
                    {"date": "2026-08-20", "employerId": 7, "employerName": "Acme"},
                    {"viewedAt": "2026-08-19", "companyId": 8, "companyName": "Beta"},
                ]
            }
        }
    )
    assert parse_resume_view_history(html, "r1", limit=1) == [
        {"resume_id": "r1", "employer_id": "7", "employer": "Acme", "viewed_at": "2026-08-20"}
    ]


def test_parse_resume_view_history_fails_closed_on_schema_drift():
    with pytest.raises(ValueError):
        parse_resume_view_history(_html({"applicantResumeViewHistory": {}}), "r1")


def test_history_deduplicates_resume_view_snapshots(tmp_path):
    history = History(tmp_path / "history.db")
    row = {"resume_id": "r1", "employer_id": "7", "employer": "Acme", "viewed_at": "2026-08-20"}
    assert history.record_resume_views([row, row]) == 1
    assert history.record_resume_views([row]) == 0
    assert len(history.resume_views()) == 1

from __future__ import annotations

import json

import pytest

from hhru_bot.history import History
from hhru_bot.resume_views import has_next_page, parse_resume_view_history

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


def test_parse_resume_view_history_preserves_hidden_same_date_events():
    html = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": [{"date": "2026-08-20"}, {"date": "2026-08-20"}]
            }
        }
    )
    rows = parse_resume_view_history(html, "r1")
    assert [row["employer"] for row in rows] == ["(скрыт #1)", "(скрыт #2)"]


def test_parse_resume_view_history_carries_hidden_offsets_across_pages():
    offsets = {}
    html = _html({"applicantResumeViewHistory": {"historyViews": [{"date": "2026-08-20"}]}})
    first = parse_resume_view_history(html, "r1", hidden_offsets=offsets)
    second = parse_resume_view_history(html, "r1", hidden_offsets=offsets)
    assert first[0]["employer"] == "(скрыт #1)"
    assert second[0]["employer"] == "(скрыт #2)"


def test_parse_resume_view_history_fails_closed_on_schema_drift():
    with pytest.raises(ValueError):
        parse_resume_view_history(_html({"applicantResumeViewHistory": {}}), "r1")


def test_has_next_page_uses_confirmed_numeric_pager():
    class Locator:
        def __init__(self, values):
            self.values = values

        def count(self):
            return len(self.values)

        def nth(self, index):
            return self.values[index]

    class Text:
        def __init__(self, value):
            self.value = value

        def inner_text(self):
            return self.value

    class Page:
        def locator(self, selector):
            if (
                "pager-next" in selector
                or "pagination-next" in selector
                or "rel='next'" in selector
            ):
                return Locator([])
            return Locator([Text("1"), Text("2")])

    assert has_next_page(Page(), 0)
    assert not has_next_page(Page(), 1)


def test_history_deduplicates_resume_view_snapshots(tmp_path):
    history = History(tmp_path / "history.db")
    row = {"resume_id": "r1", "employer_id": "7", "employer": "Acme", "viewed_at": "2026-08-20"}
    assert history.record_resume_views([row, row]) == 1
    assert history.record_resume_views([row]) == 0
    assert len(history.resume_views()) == 1

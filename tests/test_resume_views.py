from __future__ import annotations

import json

import pytest

from hhru_bot.history import History
from hhru_bot.resume_views import _canonicalize_viewed_at, has_next_page, parse_resume_view_history

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
        {
            "resume_id": "r1",
            "employer_id": "7",
            "employer": "Acme",
            "source_id": None,
            "viewed_at": "2026-08-20T00:00:00+00:00",
        }
    ]


def test_parse_resume_view_history_preserves_hidden_same_date_events():
    html = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": [
                    {"date": "2026-08-20", "id": "v1"},
                    {"date": "2026-08-20", "id": "v2"},
                ]
            }
        }
    )
    rows = parse_resume_view_history(html, "r1")
    # employer stays empty for hidden rows — the source_id carries identity for
    # dedup (history.py's view_key), never leaking into the display name (#428).
    assert [row["employer"] for row in rows] == [None, None]
    assert [row["source_id"] for row in rows] == ["v1", "v2"]


def test_canonicalize_viewed_at_treats_naive_as_utc():
    """A naive SSR timestamp and the equivalent Z-suffixed DOM one must
    canonicalize identically — otherwise the same view scraped by both
    sources dedups as two rows (#428 review)."""
    assert _canonicalize_viewed_at("2026-08-20T10:00:00") == _canonicalize_viewed_at(
        "2026-08-20T10:00:00Z"
    )


def test_canonicalize_viewed_at_normalizes_offsets_to_utc():
    """Same instant in different UTC offsets must canonicalize identically."""
    assert _canonicalize_viewed_at("2026-08-20T13:00:00+03:00") == _canonicalize_viewed_at(
        "2026-08-20T10:00:00Z"
    )


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


def test_history_dedup_ignores_mutable_employer_name(tmp_path):
    """Same employer_id + viewed_at dedups even if the display name differs
    (renamed employer, or SSR/DOM formatting drift) — #428 review."""
    history = History(tmp_path / "history.db")
    row_a = {"resume_id": "r1", "employer_id": "7", "employer": "Acme", "viewed_at": "2026-08-20"}
    row_b = {
        "resume_id": "r1",
        "employer_id": "7",
        "employer": "Acme Corp",
        "viewed_at": "2026-08-20",
    }
    assert history.record_resume_views([row_a]) == 1
    assert history.record_resume_views([row_b]) == 0
    assert len(history.resume_views()) == 1


def test_history_preserves_distinct_hidden_events_same_date(tmp_path):
    history = History(tmp_path / "history.db")
    row_a = {"resume_id": "r1", "source_id": "v1", "viewed_at": "2026-08-20"}
    row_b = {"resume_id": "r1", "source_id": "v2", "viewed_at": "2026-08-20"}
    assert history.record_resume_views([row_a, row_b]) == 2
    assert len(history.resume_views()) == 2

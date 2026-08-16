from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hhru_bot.google_calendar import GoogleCalendarError, event_payload, insert_event

pytestmark = pytest.mark.integration


def test_event_payload_requires_explicit_datetime():
    with pytest.raises(GoogleCalendarError, match="явные"):
        event_payload(
            summary="Interview", start="", end="2026-08-20T11:00:00+07:00", timezone="Asia/Bangkok"
        )


def test_event_payload_does_not_use_response_timestamp():
    payload = event_payload(
        summary="Interview",
        start="2026-08-20T10:00:00+07:00",
        end="2026-08-20T11:00:00+07:00",
        timezone="Asia/Bangkok",
    )
    assert payload["start"]["dateTime"] == "2026-08-20T10:00:00+07:00"
    assert "status_changed_at" not in payload
    assert "response_date" not in payload


def test_insert_event_disables_attendee_notifications():
    service = MagicMock()
    service.events.return_value.insert.return_value.execute.return_value = {"id": "event-1"}
    result = insert_event(service, {"summary": "Interview"})
    assert result["id"] == "event-1"
    service.events.return_value.insert.assert_called_once_with(
        calendarId="primary", body={"summary": "Interview"}, sendUpdates="none"
    )

"""Small, explicit Google Calendar client for manually confirmed events.

This module deliberately has no connection to ``responses``.  In particular,
an invitation's observation/status timestamp is not an interview start time.
Callers must provide an explicit RFC3339 start and end.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class GoogleCalendarError(RuntimeError):
    """A configuration or Google API error suitable for CLI output."""


def _write_token(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def credentials_from_files(credentials_path: str | Path, token_path: str | Path):
    """Load or interactively obtain Google credentials.

    Google dependencies are lazy so normal hh.ru commands remain usable without
    installing the optional Calendar extra.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GoogleCalendarError(
            "Google Calendar недоступен. Установите: pip install -e '.[calendar]'"
        ) from exc

    credentials_path = Path(credentials_path)
    token_path = Path(token_path)
    credentials = None
    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), [CALENDAR_SCOPE])
        except (ValueError, OSError) as exc:
            raise GoogleCalendarError(f"Некорректный Google token: {token_path}") from exc

    if credentials and credentials.valid:
        return credentials
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise GoogleCalendarError(
                "Google token истёк или отозван. Запустите `calendar auth` повторно."
            ) from exc
    else:
        if not credentials_path.is_file():
            raise GoogleCalendarError(f"OAuth client credentials не найдены: {credentials_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), [CALENDAR_SCOPE])
        credentials = flow.run_local_server(port=0, open_browser=True)

    _write_token(token_path, credentials.to_json())
    return credentials


def build_service(credentials):
    """Build the Calendar API service, importing the optional client lazily."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleCalendarError(
            "Google Calendar недоступен. Установите: pip install -e '.[calendar]'"
        ) from exc
    return build("calendar", "v3", credentials=credentials)


def event_payload(
    *,
    summary: str,
    start: str,
    end: str,
    timezone: str,
    description: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Build an event from explicitly supplied RFC3339 times."""
    if not summary.strip():
        raise GoogleCalendarError("Название события не может быть пустым")
    if not start.strip() or not end.strip():
        raise GoogleCalendarError("Нужны явные --start и --end в формате RFC3339")
    if not timezone.strip():
        raise GoogleCalendarError("Timezone не может быть пустым")
    payload: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    if description:
        payload["description"] = description
    if location:
        payload["location"] = location
    return payload


def insert_event(service, payload: dict[str, Any], calendar_id: str = "primary") -> dict[str, Any]:
    """Insert one user-confirmed event without notifying attendees."""
    if not calendar_id.strip():
        raise GoogleCalendarError("Calendar ID не может быть пустым")
    try:
        return (
            service.events()
            .insert(calendarId=calendar_id, body=payload, sendUpdates="none")
            .execute()
        )
    except Exception as exc:
        raise GoogleCalendarError(f"Не удалось создать событие Google Calendar: {exc}") from exc

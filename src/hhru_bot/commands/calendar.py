"""Manual Google Calendar commands (#64).

These commands never inspect ``responses`` and never infer an event time from
``response_date`` or ``status_changed_at``.  Creating an event requires the
user to supply explicit start/end timestamps.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_CREDENTIALS = Path("data/google_calendar/client_secret.json")
DEFAULT_TOKEN = Path("data/google_calendar/token.json")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "calendar",
        help="Ручная авторизация и создание Google Calendar event",
        description=(
            "Создание события только по явно подтверждённым --start/--end. "
            "Автоматического триггера из responses нет."
        ),
    )
    actions = parser.add_subparsers(dest="calendar_action", required=True)

    auth = actions.add_parser("auth", help="Получить или обновить Google OAuth token")
    _add_oauth_paths(auth)
    auth.set_defaults(func=authorize)

    event = actions.add_parser("event", help="Создать одно событие по явному времени")
    _add_oauth_paths(event)
    event.add_argument("--calendar-id", default="primary")
    event.add_argument("--summary", required=True)
    event.add_argument(
        "--start",
        required=True,
        help="Начало, RFC3339, например 2026-08-20T10:00:00+07:00",
    )
    event.add_argument("--end", required=True, help="Конец, RFC3339")
    event.add_argument("--timezone", default="UTC")
    event.add_argument("--description")
    event.add_argument("--location")
    event.add_argument("--dry-run", action="store_true", help="Показать payload без OAuth и записи")
    event.set_defaults(func=create_event)


def _add_oauth_paths(parser) -> None:
    parser.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
    parser.add_argument("--token", default=str(DEFAULT_TOKEN))


def authorize(args: argparse.Namespace) -> None:
    from ..google_calendar import GoogleCalendarError, credentials_from_files

    try:
        credentials_from_files(args.credentials, args.token)
    except GoogleCalendarError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"[OK] Google token сохранён: {args.token}")


def create_event(args: argparse.Namespace) -> None:
    from ..google_calendar import (
        GoogleCalendarError,
        build_service,
        credentials_from_files,
        event_payload,
        insert_event,
    )

    try:
        payload = event_payload(
            summary=args.summary,
            start=args.start,
            end=args.end,
            timezone=args.timezone,
            description=args.description,
            location=args.location,
        )
        if args.dry_run:
            print("[DRY-RUN] Google Calendar event payload:")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        credentials = credentials_from_files(args.credentials, args.token)
        event = insert_event(build_service(credentials), payload, args.calendar_id)
    except GoogleCalendarError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"[OK] Google Calendar event создан: {event.get('htmlLink') or event.get('id', '?')}")

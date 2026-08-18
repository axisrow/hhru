"""Read the authenticated account profile from hh.ru.

This is optional enrichment performed after a successful login.  A missing or
ambiguous DOM field is deliberately not treated as an empty value: only a
single locator with non-empty text may reach ``History``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import HH_BASE_URL, goto_hh
from .history import History
from .selector_groups import account_profile

PROFILE_FIELDS = (
    ("Имя", account_profile.ACCOUNT_PROFILE_FIRST_NAME),
    ("Фамилия", account_profile.ACCOUNT_PROFILE_LAST_NAME),
    ("Город", account_profile.ACCOUNT_PROFILE_CITY),
    ("Телефон", account_profile.ACCOUNT_PROFILE_PHONE),
    ("Email", account_profile.ACCOUNT_PROFILE_EMAIL),
)


def _warn(message: str) -> None:
    print(f"[WARN] Профиль: {message}")


def read_account_profile(page: Page, history_path: str | Path) -> int:
    """Read profile fields, returning the number safely persisted.

    Navigation and DOM failures are fail-open for the surrounding login flow.
    Values are fail-closed: count must be exactly one and ``inner_text`` must
    be non-empty before writing an ``hh_ru`` field.
    """
    try:
        goto_hh(page, f"{HH_BASE_URL}{account_profile.ACCOUNT_PROFILE_PATH}")
    except PlaywrightError as exc:
        _warn(f"страница недоступна: {exc}")
        print("[INFO] Профиль обновлён: 0 полей")
        return 0

    # A successful navigation alone is not enough: hh.ru can return an error,
    # auth, or not-yet-rendered page with a zero count for every field. The
    # confirmed name card is the profile-specific positive marker that permits
    # absence-based cleanup below.
    try:
        page_marker = page.locator(account_profile.ACCOUNT_PROFILE_FIRST_NAME)
        if page_marker.count() != 1:
            _warn("страница профиля не подтверждена — старые данные сохранены")
            print("[INFO] Профиль обновлён: 0 полей")
            return 0
    except PlaywrightError as exc:
        _warn(f"страница профиля не подтверждена: {exc}")
        print("[INFO] Профиль обновлён: 0 полей")
        return 0

    try:
        history = History(history_path)
    except (OSError, sqlite3.Error) as exc:
        _warn(f"не удалось открыть историю: {exc}")
        print("[INFO] Профиль обновлён: 0 полей")
        return 0
    updated = 0
    for question_key, selector in PROFILE_FIELDS:
        try:
            locator = page.locator(selector)
            count = locator.count()
            if count != 1:
                if count == 0:
                    # A successfully loaded profile can legitimately omit a
                    # private/unfilled field. Remove a previous hh_ru value so
                    # it cannot leak into a later account's form fill.
                    history.delete_profile_field(question_key, source="hh_ru")
                _warn(f"поле «{question_key}» не подтверждено (найдено: {count})")
                continue
            value = locator.inner_text().strip()
            if not value:
                history.delete_profile_field(question_key, source="hh_ru")
                _warn(f"поле «{question_key}» пустое")
                continue
            history.upsert_profile_field(question_key, value, source="hh_ru")
            updated += 1
        except (PlaywrightError, OSError, ValueError, sqlite3.Error) as exc:
            _warn(f"поле «{question_key}» не прочитано: {exc}")

    print(f"[INFO] Профиль обновлён: {updated} полей")
    return updated

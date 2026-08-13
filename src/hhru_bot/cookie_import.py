"""Import the hh.ru cookies from the user's Chrome profile."""

from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

CHROME_EPOCH_OFFSET = 11_644_473_600
MAX_PLAYWRIGHT_EXPIRES = 253_402_300_799  # 9999-12-31T23:59:59Z
SAMESITE = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}


def chrome_cookie_file(profile: Path | None = None) -> Path:
    profile = profile or Path.home() / "Library/Application Support/Google/Chrome/Default"
    return profile / "Cookies"


def chrome_expires_to_playwright(expires_utc: int | float) -> float:
    """Convert Chrome's microseconds since 1601 to Playwright seconds."""
    if expires_utc == 0:
        return -1
    try:
        expires = float(expires_utc) / 1_000_000 - CHROME_EPOCH_OFFSET
    except (OverflowError, ValueError) as exc:
        raise ValueError("Некорректный срок действия cookie Chrome") from exc
    if not math.isfinite(expires) or expires < 0 or expires > MAX_PLAYWRIGHT_EXPIRES:
        raise ValueError("Срок действия cookie Chrome выходит за допустимый диапазон")
    return expires


def chrome_samesite_to_playwright(samesite: int) -> str:
    try:
        return SAMESITE[int(samesite)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Неизвестное значение SameSite Chrome: {samesite!r}") from exc


def build_storage_state(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build storage_state from already-decrypted, domain-filtered DB rows."""
    cookies = []
    for row in rows:
        host = str(row["host_key"])
        # Keep this guard even though read_chrome_cookies filters in SQL.  It is
        # a defence against an accidentally broadened query and makes the
        # privacy boundary explicit for callers/tests supplying fake rows.
        if host not in {"hh.ru", ".hh.ru"} and not host.endswith(".hh.ru"):
            continue
        cookies.append(
            {
                "name": str(row["name"]),
                "value": str(row["value"]),
                "domain": host,
                "path": str(row["path"]),
                "expires": chrome_expires_to_playwright(row["expires_utc"]),
                "httpOnly": bool(row["is_httponly"]),
                "secure": bool(row["is_secure"]),
                "sameSite": chrome_samesite_to_playwright(row["samesite"]),
            }
        )
    return {"cookies": cookies, "origins": []}


def read_chrome_cookies(cookie_file: Path | str) -> list[dict[str, Any]]:
    """Decrypt only hh.ru rows from Chrome's cookie DB.

    browser-cookie3 supplies the platform-specific Chrome decryption and
    Keychain handling.  The SQL query remains here so SameSite/HttpOnly
    metadata survives and non-hh.ru rows are never selected into Python.
    """
    import browser_cookie3

    chrome = browser_cookie3.Chrome(cookie_file=str(cookie_file), domain_name="hh.ru")
    with browser_cookie3._DatabaseConnetion(str(cookie_file)) as connection:
        connection.text_factory = browser_cookie3._text_factory
        cursor = connection.cursor()
        has_integrity = chrome._has_integrity_check_for_cookie_domain(cursor)
        cursor.execute(
            """SELECT host_key, path, is_secure, expires_utc, name, value,
                      encrypted_value, is_httponly, samesite
                 FROM cookies
                WHERE host_key = ? OR host_key LIKE ?""",
            ("hh.ru", "%.hh.ru"),
        )
        rows = []
        for row in cursor.fetchall():
            host, path, secure, expires, name, value, encrypted, http_only, samesite = row
            rows.append(
                {
                    "host_key": host,
                    "path": path,
                    "is_secure": secure,
                    "expires_utc": expires,
                    "name": name,
                    "value": chrome._decrypt(value, encrypted, has_integrity),
                    "is_httponly": http_only,
                    "samesite": samesite,
                }
            )
    return rows


def write_storage_state(state: dict[str, Any], destination: Path | str) -> Path | None:
    """Write state, preserving existing state and an existing backup."""
    destination = Path(destination)
    backup: Path | None = None
    if destination.exists():
        candidate = destination.with_name(destination.name + ".bak")
        index = 1
        while candidate.exists():
            candidate = destination.with_name(destination.name + f".bak.{index}")
            index += 1
        shutil.copy2(destination, candidate)
        backup = candidate
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: an interrupted/failed write must never leave the
    # active session truncated (Codex review, PR #168) — the backup above
    # already preserves the old session, but only a temp write + atomic
    # os.replace() keeps `destination` itself intact if this step fails.
    #
    # The temp file is a NEW inode, so it starts with the process umask
    # (typically 0644) rather than destination's mode — os.replace() would
    # otherwise silently widen a restrictive (0600) session file to
    # group/world-readable (Codex re-review, PR #168). storage_state_file
    # holds a bearer token (hhtoken); force owner-only permissions on the
    # temp file explicitly instead of relying on umask.
    tmp = destination.with_name(destination.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, destination)
    return backup

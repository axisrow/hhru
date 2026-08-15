"""Import the hh.ru cookies from the user's Chrome profile."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

CHROME_EPOCH_OFFSET = 11_644_473_600
MAX_PLAYWRIGHT_EXPIRES = 253_402_300_799  # 9999-12-31T23:59:59Z
SAMESITE = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}

DEFAULT_CHROME_PROFILES_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
DEFAULT_CHROME_PROFILE_NAME = "Default"


def resolve_chrome_profile(profile: Path | None = None) -> Path:
    """Resolve the Chrome profile directory.

    Явный существующий путь — как есть (абсолютный или относительный cwd).
    Имя профиля (`Default`, `Profile 1`) без существующего cwd-пути — от
    стандартного корня профилей Chrome (macOS). Иначе — как введено:
    несуществующий путь даст понятную ошибку "No such file or directory"
    от read_chrome_cookies (find-one-shot-20260815: `--profile Default`
    резолвился от cwd и падал, хотя профиль есть).
    """
    profile = profile or Path(DEFAULT_CHROME_PROFILE_NAME)
    if not profile.exists() and not profile.is_absolute():
        candidate = DEFAULT_CHROME_PROFILES_ROOT / profile
        # `len(profile.parts) == 1` — a bare profile name (e.g. "Default"),
        # not a multi-segment relative path like "foo/Default" (cycle-review
        # PR #173 round 2, claude/review): profile.name matches only the
        # last path segment, so without this guard a relative path the
        # caller meant literally would be silently redirected under the
        # Chrome profiles root just because it ends in "Default".
        if candidate.exists() or (
            len(profile.parts) == 1 and profile.name == DEFAULT_CHROME_PROFILE_NAME
        ):
            return candidate
    return profile


def chrome_cookie_file(profile: Path | None = None) -> Path:
    return resolve_chrome_profile(profile) / "Cookies"


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
        # Exclusive create (O_CREAT|O_EXCL), not candidate.exists(): a
        # pre-planted symlink at the guessable `<destination>.bak` name can be
        # *dangling*, so exists() (which follows symlinks) reports False and
        # shutil.copy2() would then follow the symlink and write the session
        # secret into the attacker's target (cycle-review PR #173 round 1,
        # codex — same attack class #171 closed for the `.tmp` path, left
        # open here). O_EXCL fails on any existing name, symlink or not, so
        # the backup inode is always a fresh file this call created.
        #
        # Write through the fd without closing it first (round 2, codex —
        # closing the fd and then calling shutil.copy2(path) reopened a TOCTOU
        # window: an attacker able to modify the storage directory could
        # unlink the just-created backup and replace it with a symlink before
        # copy2() ran). Reading and writing while the exclusively-created fd
        # stays open means there is no path lookup left to redirect.
        candidate = destination.with_name(destination.name + ".bak")
        index = 1
        while True:
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                candidate = destination.with_name(destination.name + f".bak.{index}")
                index += 1
                continue
            break
        try:
            source_stat = destination.stat()
            with os.fdopen(fd, "wb") as handle:
                handle.write(destination.read_bytes())
                # fd-relative metadata copy (not shutil.copystat(path)): a
                # path-based call here would reopen the same TOCTOU window
                # this fix closes above — os.futimens/os.fchmod act on the
                # already-open descriptor, no path lookup left to redirect.
                os.utime(handle.fileno(), ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        except BaseException:
            # round 2 (claude/review): a failed copy must not leave an empty
            # `.bak` file — O_EXCL on the next run would treat that name as
            # permanently taken and the backup numbering would drift forever.
            candidate.unlink(missing_ok=True)
            raise
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
    # group/world-readable. storage_state_file holds a bearer token
    # (hhtoken); mkstemp() creates it with 0o600 from the very first byte
    # (O_EXCL built in) AND with an unpredictable name, so a pre-planted
    # symlink at a guessable `<destination>.tmp` can no longer redirect the
    # secret into an attacker-controlled file (#171 — Codex review 3 of
    # PR #168, merged without this fix).
    fd, tmp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=destination.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        # Defence-in-depth (#171): mkstemp already guarantees O_EXCL|O_CREAT
        # with 0o600, but a compromised/widened mode here must fail loudly
        # instead of leaking the bearer token through a group/world-readable
        # temp file.
        mode = os.fstat(fd).st_mode & 0o777
        if mode != 0o600:
            os.close(fd)
            raise OSError(f"temp-файл сессии создан с режимом {mode:o} вместо 0600")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, destination)
    return backup

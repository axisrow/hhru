"""Offline, deterministic incident bundles assembled from existing artifacts."""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

_SECRET = re.compile(
    r"(?i)(['\"]?(?:cookie|authorization|token|password|secret|api[_-]?key|csrf[_-]?token|session[_-]?id)['\"]?)\s*[:=]\s*[^\r\n,}]*"
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_URL = re.compile(r"https?://[^\s\]}>]+")
_PATH = re.compile(r"/(?:Users|home|private/var)/[^\s\]}>]+")
_PHONE = re.compile(
    r"(?<!\w)(?:\+?[89][\d ()-]{8,}\d|\(\d{3}\)[ -]?\d{3}[ -]?\d{2}[ -]?\d{2})(?!\w)"
)
_MESSAGE = re.compile(
    r"(?is)(cover letter|message|letter|письм\w*|сообщен\w*)\s*[:=].*?(?=\s+[\w-]+\s*[:=]|$)"
)


def redact(value: str) -> str:
    value = _SECRET.sub(lambda m: m.group(1) + "=[REDACTED]", value)
    value = _EMAIL.sub("[REDACTED_EMAIL]", value)
    value = _URL.sub("[REDACTED_URL]", value)
    value = _PATH.sub("[REDACTED_PATH]", value)
    value = _PHONE.sub("[REDACTED_PHONE]", value)
    return _MESSAGE.sub(lambda m: m.group(1) + "=[REDACTED]", value)


def _same_path(left: Path, right: Path) -> bool:
    """Compare aliases too; ``resolve`` alone misses existing hard links."""
    left, right = left.expanduser(), right.expanduser()
    if left.resolve() == right.resolve():
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _open_history_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    return sqlite3.connect(f"file:{quote(str(resolved))}?mode=ro", uri=True)


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


_LOG_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?)\s+"
    r"\[(?P<level>[A-Z]+)\]\s+(?P<logger>[\w.]+):"
)
_SAFE_EVENTS = (
    ("run", "[RUN]"),
    ("probe", "[PROBE]"),
    ("verify", "[VERIFY]"),
    ("selector", "селектор"),
    ("warning", "[WARN"),
    ("error", "ошиб"),
)


def _safe_log_line(line: str) -> dict[str, str] | None:
    """Export only fixed metadata and a classified event, never the message."""
    match = _LOG_LINE.match(line)
    if not match:
        return None
    event = "log"
    for name, marker in _SAFE_EVENTS:
        if marker.casefold() in line.casefold():
            event = name
            break
    return {
        "timestamp": match["timestamp"].replace(" ", "T"),
        "level": match["level"],
        "logger": match["logger"],
        "event": event,
    }


def _safe_dom(path: Path) -> dict[str, Any]:
    """Return structure-only evidence; never expose text or arbitrary attrs."""
    if not path.is_file():
        return {"path": path.name, "available": False}
    text = path.read_text(encoding="utf-8", errors="replace")
    tags = re.findall(r"<([a-z][\w-]*)([^>]*)>", text, re.I)
    allowed = {"data-qa", "role"}
    nodes = []
    for tag, attrs in tags[:100]:
        item = {"tag": tag.lower()}
        for name, val in re.findall(r'([\w:-]+)\s*=\s*["\']([^"\']*)', attrs):
            if name.lower() in allowed:
                item[name.lower()] = redact(val)[:120]
        nodes.append(item)
    return {"path": path.name, "available": True, "nodes": nodes}


def build_bundle(
    history: Path,
    run_id: str | None = None,
    log_path: Path | None = None,
    dom_dir: Path | None = None,
    log_lines: int = 80,
) -> dict[str, Any]:
    with _open_history_read_only(history) as db:
        db.row_factory = sqlite3.Row
        q = (
            "SELECT * FROM command_runs WHERE run_id=?"
            if run_id
            else "SELECT * FROM command_runs ORDER BY started_at DESC LIMIT 1"
        )
        row = db.execute(q, (run_id,) if run_id else ()).fetchone()
        if row is None:
            raise ValueError(f"command run not found: {run_id or 'latest'}")
        run = dict(row)
        if run.get("detail"):
            run["detail"] = str(run["detail"]).split(":", 1)[0]
    lines: list[dict[str, str]] = []
    if log_path and log_path.is_file():
        start = _parse_timestamp(run.get("started_at"))
        finish = _parse_timestamp(run.get("finished_at"))
        # Log formatter keeps whole seconds while SQLite stores microseconds.
        # Widen only to the representable log precision, otherwise short runs
        # beginning mid-second lose their first evidence line.
        if start:
            start = start.replace(microsecond=0)
        if finish:
            finish = finish.replace(microsecond=0)
        candidates = []
        for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            safe = _safe_log_line(raw)
            stamp = _parse_timestamp(safe["timestamp"]) if safe else None
            if safe and start and stamp and stamp >= start and (finish is None or stamp <= finish):
                candidates.append(safe)
        lines = candidates[-log_lines:]
    snapshots = []
    if dom_dir and dom_dir.is_dir():
        for p in sorted(dom_dir.glob("*.html")):
            metadata_path = p.with_suffix(".json")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(metadata, Mapping)
                and metadata.get("run_id") == run.get("run_id")
                and metadata.get("artifact") == p.name
            ):
                snapshots.append(_safe_dom(p))
        snapshots = snapshots[:5]
    return {
        "schema_version": "1.0.0",
        "bundle_version": "1",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "run": run,
        "log_tail": lines,
        "selectors": {"hits": [], "misses": [], "evidence": []},
        "snapshots": snapshots,
    }


def export_bundle(**kwargs: Any) -> str:
    return json.dumps(build_bundle(**kwargs), ensure_ascii=False, sort_keys=True, indent=2) + "\n"

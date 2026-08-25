"""Offline, deterministic incident bundles assembled from existing artifacts."""

from __future__ import annotations

import json
import platform
import re
import sqlite3
from pathlib import Path
from typing import Any

_SECRET = re.compile(
    r"(?i)(['\"]?(?:cookie|authorization|token|password|secret)['\"]?)\s*[:=]\s*[^\r\n,}]*"
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_URL = re.compile(r"https?://[^\s\]}>]+")
_PATH = re.compile(r"/(?:Users|home|private/var)/[^\s\]}>]+")
_PHONE = re.compile(r"(?<!\w)(?:\+\d[\d ()-]{8,}\d|\d{10,})(?!\w)")
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
    with sqlite3.connect(history) as db:
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
    lines = []
    if log_path and log_path.is_file():
        lines = [
            redact(x.rstrip("\n"))
            for x in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[
                -log_lines:
            ]
        ]
    snapshots = []
    if dom_dir and dom_dir.is_dir():
        snapshots = [
            _safe_dom(p)
            for p in sorted(dom_dir.glob("*.html"))
            if run.get("run_id") and str(run["run_id"]) in p.name
        ][:5]
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

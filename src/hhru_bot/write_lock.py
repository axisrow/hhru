"""Inter-process lock for commands that can mutate hh.ru."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout


class WriteLockBusy(RuntimeError):
    """Another hhru-bot write command is already running."""

    def __init__(self, owner: dict | None = None):
        self.owner = owner or {}
        super().__init__("another write command is already running")


def _owner_path(path: Path) -> Path:
    """Return the adjacent file that stores lock-owner diagnostics."""
    return path.with_name(f"{path.name}.owner")


def _read_owner(path: Path) -> dict:
    try:
        owner = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}
    return owner if isinstance(owner, dict) else {}


def _write_owner(path: Path, owner: dict) -> None:
    path.write_text(json.dumps(owner, sort_keys=True), encoding="utf-8")
    with path.open("r+b") as owner_file:
        owner_file.seek(0, os.SEEK_END)
        owner_file.flush()
        os.fsync(owner_file.fileno())


@contextlib.contextmanager
def acquire_write_lock(path: Path, *, command: str = "unknown") -> Iterator[None]:
    """Hold an exclusive, non-blocking lock until the command finishes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(path, timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise WriteLockBusy(_read_owner(_owner_path(path))) from exc
    try:
        owner = {
            "pid": os.getpid(),
            "command": command,
            "started_at": datetime.now(UTC).isoformat(),
        }
        _write_owner(_owner_path(path), owner)
        yield
    finally:
        lock.release()

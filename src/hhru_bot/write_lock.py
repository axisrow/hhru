"""Advisory inter-process lock for commands that can mutate hh.ru."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path


class WriteLockBusy(RuntimeError):
    """Another hhru-bot write command is already running."""

    def __init__(self, owner: dict | None = None):
        self.owner = owner or {}
        super().__init__("another write command is already running")


@contextlib.contextmanager
def acquire_write_lock(path: Path, *, command: str = "unknown") -> Iterator[None]:
    """Hold an exclusive, non-blocking lock until the command finishes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.seek(0)
            try:
                owner = json.loads(lock_file.read() or "{}")
            except (json.JSONDecodeError, OSError):
                owner = {}
            raise WriteLockBusy(owner) from exc
        try:
            owner = {
                "pid": os.getpid(),
                "command": command,
                "started_at": datetime.now(UTC).isoformat(),
            }
            lock_file.seek(0)
            lock_file.truncate()
            json.dump(owner, lock_file, sort_keys=True)
            lock_file.flush()
            os.fsync(lock_file.fileno())
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

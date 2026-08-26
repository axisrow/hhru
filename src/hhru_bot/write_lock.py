"""Advisory inter-process lock for commands that can mutate hh.ru."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import msvcrt as _lock_backend
else:
    import fcntl as _lock_backend


# ``msvcrt.locking`` locks a byte range rather than the whole file. Keep the
# range outside the JSON payload: Windows mandatory locking can otherwise
# prevent a contender from reading the owner metadata after a failed lock.
_WINDOWS_LOCK_OFFSET = 4096


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
        if _IS_WINDOWS:
            # ``a+`` creates the file but writes only at EOF. Reserve a stable
            # byte at the end of the owner payload before locking it.
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() <= _WINDOWS_LOCK_OFFSET:
                lock_file.write(" " * (_WINDOWS_LOCK_OFFSET + 1 - lock_file.tell()))
                lock_file.flush()
                os.fsync(lock_file.fileno())
            lock_file.seek(_WINDOWS_LOCK_OFFSET)
        try:
            if _IS_WINDOWS:
                _lock_backend.locking(lock_file.fileno(), _lock_backend.LK_NBLCK, 1)
            else:
                _lock_backend.flock(
                    lock_file.fileno(), _lock_backend.LOCK_EX | _lock_backend.LOCK_NB
                )
        except OSError as exc:
            lock_file.seek(0)
            try:
                # Do not read through the mandatory Windows lock byte. The
                # owner JSON is deliberately stored before it.
                owner_payload = (
                    lock_file.read(_WINDOWS_LOCK_OFFSET) if _IS_WINDOWS else lock_file.read()
                )
                owner = json.loads(owner_payload or "{}")
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
            if _IS_WINDOWS:
                # Keep the lock byte present and leave only JSON whitespace
                # after the payload, so the file remains json.loads-compatible.
                lock_file.write(" " * max(0, _WINDOWS_LOCK_OFFSET + 1 - lock_file.tell()))
            lock_file.flush()
            os.fsync(lock_file.fileno())
            yield
        finally:
            if _IS_WINDOWS:
                lock_file.seek(_WINDOWS_LOCK_OFFSET)
                _lock_backend.locking(lock_file.fileno(), _lock_backend.LK_UNLCK, 1)
            else:
                _lock_backend.flock(lock_file.fileno(), _lock_backend.LOCK_UN)

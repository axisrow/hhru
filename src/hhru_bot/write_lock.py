"""Advisory inter-process lock for commands that can mutate hh.ru."""

from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Iterator
from pathlib import Path


class WriteLockBusy(RuntimeError):
    """Another hhru-bot write command is already running."""


@contextlib.contextmanager
def acquire_write_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock until the command finishes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WriteLockBusy from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

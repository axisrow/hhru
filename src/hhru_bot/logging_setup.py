from __future__ import annotations

import logging
import os
import shutil
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock

# Логи — относительно cwd (точки запуска), не относительно пакета: после
# `pip install` пакет в site-packages, писать логи туда нельзя. См. cli.py.
# Внутри data/ (#133): все изменяемые данные проекта в одной папке, покрытой
# .gitignore одной строкой. Единая точка — probe.PROBE_LOG_DIR наследует её.
LOG_DIR = Path.cwd() / "data" / "logs"

# 10 MiB is large enough for a useful diagnostic window while putting a
# bounded ceiling on the active file.  The backup count is deliberately kept
# in the handler configuration for compatibility with RotatingFileHandler,
# but _PreservingRotatingFileHandler never uses it as a retention limit.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 1000


class _PreservingRotatingFileHandler(RotatingFileHandler):
    """Size-based rotation which never removes an archived log.

    ``RotatingFileHandler`` normally shifts ``.1`` ... ``.N`` and deletes the
    oldest file once ``backupCount`` is reached.  Log history is user data for
    this application, so archives are instead assigned the next unused
    numeric suffix.  Cleanup is intentionally a manual user decision.
    """

    _rollover_lock = threading.Lock()

    def doRollover(self) -> None:
        with self._rollover_lock, FileLock(self._rotation_lock_path):
            self._do_rollover_locked()

    def emit(self, record: logging.LogRecord) -> None:
        """Serialize writes as well as rollover across processes.

        The lock must cover the record write: on Windows another process may
        keep the active file open, so rename-based rollover is not available.
        Copy-truncating under the same lock lets all processes keep writing
        without dropping the record which triggered rotation.
        """
        try:
            with self._rollover_lock, FileLock(self._rotation_lock_path):
                self._reopen_if_path_was_replaced_locked()
                if self.shouldRollover(record):
                    self._do_rollover_locked()
                logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)

    def _do_rollover_locked(self) -> None:
        self._reopen_if_path_was_replaced_locked()
        # During an external rename rotation there can be a short interval in
        # which the active path does not exist. Never truncate the still-open
        # descriptor in that interval: it is already the archived segment.
        if not os.path.exists(self.baseFilename):
            return
        if self.stream is not None:
            self.stream.flush()
            self.stream.close()
            self.stream = None

        try:
            # Keep the copy and truncate on the same read/write descriptor. In
            # particular, do not copy by path and truncate self.stream: an
            # external rename between those operations could erase a segment
            # which is no longer the active file.
            with open(self.baseFilename, "r+b") as active:
                archive = self._create_archive_locked()
                active.seek(0)
                with open(archive, "wb") as target:
                    shutil.copyfileobj(active, target)
                active.seek(0)
                active.truncate()
        finally:
            if not self.delay:
                self.stream = self._open()

    def _reopen_if_path_was_replaced_locked(self) -> None:
        """Rebind the writer after an external rename-based rotation."""
        if self.stream is None:
            return
        try:
            current = os.fstat(self.stream.fileno())
            active = os.stat(self.baseFilename)
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == (active.st_dev, active.st_ino):
            return
        self.stream.flush()
        self.stream.close()
        self.stream = self._open()

    def _create_archive_locked(self) -> str:
        index = 1
        while True:
            archive = f"{self.baseFilename}.{index}"
            try:
                fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                index += 1
                continue
            os.close(fd)
            return archive

    @property
    def _rotation_lock_path(self) -> str:
        """A hidden sidecar path which is not mistaken for a log archive."""
        base = Path(self.baseFilename)
        return str(base.with_name(f".{base.name}.rotate.lock"))


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "hhru_bot.log"

    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger("hhru_bot")
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    file_handler = _PreservingRotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

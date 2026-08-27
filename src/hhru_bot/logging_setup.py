from __future__ import annotations

import logging
import os
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
            if self.stream is not None:
                self.stream.flush()
                self.stream.close()
                self.stream = None

            if os.path.exists(self.baseFilename):
                archive = self._next_archive_path()
                os.replace(self.baseFilename, archive)

            if not self.delay:
                self.stream = self._open()

    @property
    def _rotation_lock_path(self) -> str:
        """A hidden sidecar path which is not mistaken for a log archive."""
        base = Path(self.baseFilename)
        return str(base.with_name(f".{base.name}.rotate.lock"))

    def _next_archive_path(self) -> str:
        index = 1
        while True:
            archive = f"{self.baseFilename}.{index}"
            if not os.path.exists(archive):
                return archive
            index += 1


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

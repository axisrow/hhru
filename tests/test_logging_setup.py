"""Tests for bounded, lossless application log rotation."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic, sleep

import pytest

from hhru_bot import logging_setup

pytestmark = pytest.mark.integration


def _close_hhru_handlers() -> None:
    logger = logging.getLogger("hhru_bot")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()


def test_setup_logging_rotates_without_deleting_archives(tmp_path, monkeypatch):
    """All rotated segments survive, even beyond the configured backup count."""
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path)
    monkeypatch.setattr(logging_setup, "LOG_MAX_BYTES", 128)
    monkeypatch.setattr(logging_setup, "LOG_BACKUP_COUNT", 2)
    logger = logging.getLogger("hhru_bot")
    previous_level = logger.level

    try:
        logging_setup.setup_logging()
        handler = next(
            handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)
        )
        assert handler.maxBytes == 128
        assert handler.backupCount == 2

        messages = [f"rotation-message-{index}-" + "x" * 80 for index in range(6)]
        for message in messages:
            logger.info(message)

        log_file = tmp_path / "hhru_bot.log"
        archives = sorted(
            tmp_path.glob("hhru_bot.log.*"),
            key=lambda path: int(path.name.rsplit(".", 1)[1]),
        )
        assert len(archives) == len(messages) - 1
        all_segments = "\n".join(path.read_text(encoding="utf-8") for path in [*archives, log_file])
        for message in messages:
            assert message in all_segments
    finally:
        _close_hhru_handlers()
        logger.setLevel(previous_level)


def test_setup_logging_preserves_console_and_level(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path)
    logger = logging.getLogger("hhru_bot")
    previous_level = logger.level

    try:
        logging_setup.setup_logging(verbose=True)
        assert logger.level == logging.DEBUG
        assert any(type(handler) is logging.StreamHandler for handler in logger.handlers)
        assert any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers)
    finally:
        _close_hhru_handlers()
        logger.setLevel(previous_level)


def test_concurrent_process_rollovers_allocate_distinct_archives(tmp_path: Path):
    """Separate CLI processes cannot overwrite each other's archive."""
    log_file = tmp_path / "hhru_bot.log"
    log_file.write_text("original\n", encoding="utf-8")
    child_script = """
import sys
import time
from pathlib import Path
from hhru_bot import logging_setup

log_file, ready, start = map(Path, sys.argv[1:])
handler = logging_setup._PreservingRotatingFileHandler(
    log_file, maxBytes=1, backupCount=1, encoding="utf-8"
)
ready.touch()
while not start.exists():
    time.sleep(0.01)
handler.doRollover()
handler.close()
"""
    env = os.environ.copy()
    src = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH"))))
    start = tmp_path / "start"
    processes = []
    try:
        for index in range(2):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        child_script,
                        str(log_file),
                        str(tmp_path / f"ready-{index}"),
                        str(start),
                    ],
                    env=env,
                )
            )
        deadline = monotonic() + 10
        while not all((tmp_path / f"ready-{index}").exists() for index in range(2)):
            if any(process.poll() is not None for process in processes):
                pytest.fail("a rollover child exited before becoming ready")
            if monotonic() >= deadline:
                pytest.fail("rollover children did not become ready")
            sleep(0.01)
        start.touch()
        for process in processes:
            assert process.wait(timeout=10) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()

    assert (tmp_path / "hhru_bot.log.1").read_text(encoding="utf-8") == "original\n"
    assert (tmp_path / "hhru_bot.log.2").exists()


def test_rollover_does_not_truncate_inode_replaced_externally(tmp_path: Path):
    """An external rename cannot make the handler erase the old segment."""
    log_file = tmp_path / "hhru_bot.log"
    log_file.write_text("old\n", encoding="utf-8")
    handler = logging_setup._PreservingRotatingFileHandler(
        log_file,
        maxBytes=1,
        backupCount=1,
        encoding="utf-8",
    )
    try:
        log_file.rename(tmp_path / "external.log")
        log_file.write_text("fresh\n", encoding="utf-8")
        record = logging.LogRecord("hhru_bot", logging.INFO, __file__, 1, "new", (), None)
        handler.emit(record)
    finally:
        handler.close()

    assert (tmp_path / "external.log").read_text(encoding="utf-8") == "old\n"
    assert (tmp_path / "hhru_bot.log.1").read_text(encoding="utf-8") == "fresh\n"
    assert "new" in log_file.read_text(encoding="utf-8")


def test_missing_active_path_does_not_truncate_renamed_segment(tmp_path: Path):
    """A rename/create gap must preserve the still-open archived descriptor."""
    log_file = tmp_path / "hhru_bot.log"
    log_file.write_text("old\n", encoding="utf-8")
    handler = logging_setup._PreservingRotatingFileHandler(
        log_file,
        maxBytes=1,
        backupCount=1,
        encoding="utf-8",
    )
    archived = tmp_path / "external.log"
    try:
        log_file.rename(archived)
        record = logging.LogRecord("hhru_bot", logging.INFO, __file__, 1, "new", (), None)
        handler.emit(record)
    finally:
        handler.close()

    assert archived.read_text(encoding="utf-8") == "old\nnew\n"
    assert not (tmp_path / "hhru_bot.log.1").exists()

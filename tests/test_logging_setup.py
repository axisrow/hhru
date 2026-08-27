"""Tests for bounded, lossless application log rotation."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

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

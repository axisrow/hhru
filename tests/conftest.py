"""Общие фикстуры и хелперы для characterization-тестов.

Тесты не запускают браузер — Playwright нужен только для импорта модулей,
вся тестируемая логика чистая (без Page/браузера).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hhru_bot import logging_setup
from hhru_bot.commands import log_cmd


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Страховка от записи тестов в реальный data/logs/hhru_bot.log (#131).

    До этой фикстуры изоляция держалась на дисциплине каждого отдельного теста
    (см. локальный monkeypatch в test_log_command_does_not_create_log, #129/#130) —
    не на инварианте. Здесь уводим LOG_DIR на tmp_path для ВСЕХ тестов сессии.

    DEFAULT_LOG_PATH (log_cmd.py) вычисляется на импорте модуля как
    `LOG_DIR / "hhru_bot.log"` и сам не пересчитается при подмене LOG_DIR —
    патчим его отдельно тем же tmp_path.

    Монки этой фикстуры и локальные monkeypatch внутри отдельных тестов
    накладываются безопасно (LIFO): более специфичный тестовый monkeypatch
    отменяется первым при teardown, эта фикстура — последней.
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(logging_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(log_cmd, "DEFAULT_LOG_PATH", log_dir / "hhru_bot.log")

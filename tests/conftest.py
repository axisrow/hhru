"""Общие фикстуры и хелперы для characterization-тестов.

Тесты не запускают браузер — Playwright нужен только для импорта модулей,
вся тестируемая логика чистая (без Page/браузера).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from hhru_bot import logging_setup
from hhru_bot.apply import probe
from hhru_bot.apply import steps as apply_steps
from hhru_bot.commands import log_cmd


@pytest.fixture(autouse=True)
def _block_live_browser(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Не позволяет обычным тестам случайно выйти в браузер и сеть."""
    if any(
        request.node.get_closest_marker(marker)
        for marker in ("live_read", "live_write", "live_write_danger")
    ):
        return

    import hhru_bot.browser as browser

    real_launch_context = browser.launch_context
    real_sync_playwright = browser.sync_playwright

    @contextmanager
    def _blocked_launch_context(*args: object, **kwargs: object):
        # Разрешаем только тесту, явно подменившему сам Playwright (например,
        # test_browser_navigation); настоящий движок остаётся закрыт.
        if browser.sync_playwright is not real_sync_playwright:
            with real_launch_context(*args, **kwargs) as context:
                yield context
            return
        raise RuntimeError(
            "launch_context заблокирован: тест должен использовать моки; "
            "для живого hh.ru нужен маркер live_read, live_write или live_write_danger"
        )

    for module in tuple(sys.modules.values()):
        if module is None or module is browser:
            continue
        if getattr(module, "launch_context", None) is real_launch_context:
            monkeypatch.setattr(module, "launch_context", _blocked_launch_context)
    monkeypatch.setattr(browser, "launch_context", _blocked_launch_context)


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Страховка от записи тестов в реальный data/logs/hhru_bot.log (#131).

    До этой фикстуры изоляция держалась на дисциплине каждого отдельного теста
    (см. локальный monkeypatch в test_log_command_does_not_create_log, #129/#130) —
    не на инварианте. Здесь уводим LOG_DIR на tmp_path для ВСЕХ тестов сессии.

    DEFAULT_LOG_PATH (log_cmd.py), PROBE_LOG_DIR (apply/probe.py) и steps.LOG_DIR
    (apply/steps.py, дампы #195/#207) вычисляются/импортируются на импорте модуля
    как `LOG_DIR / ...` / `from ..logging_setup import LOG_DIR` и сами не
    пересчитаются при подмене LOG_DIR — патчим их отдельно тем же tmp_path.

    Монки этой фикстуры и локальные monkeypatch внутри отдельных тестов
    накладываются безопасно (LIFO): более специфичный тестовый monkeypatch
    отменяется первым при teardown, эта фикстура — последней.
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(logging_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(log_cmd, "DEFAULT_LOG_PATH", log_dir / "hhru_bot.log")
    monkeypatch.setattr(probe, "PROBE_LOG_DIR", log_dir)
    monkeypatch.setattr(apply_steps, "LOG_DIR", log_dir)

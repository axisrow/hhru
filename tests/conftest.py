"""Общие фикстуры и хелперы для characterization-тестов.

Тесты не запускают браузер — Playwright нужен только для импорта модулей,
вся тестируемая логика чистая (без Page/браузера).
"""

from __future__ import annotations

import sys
from pathlib import Path

import playwright.sync_api
import pytest
from playwright.sync_api._context_manager import PlaywrightContextManager

from hhru_bot import logging_setup
from hhru_bot.apply import probe
from hhru_bot.apply import steps as apply_steps
from hhru_bot.commands import log_cmd

_LIVE_MARKERS = ("live_read", "live_write", "live_write_danger")

_BLOCKED_MESSAGE = (
    "Playwright заблокирован: тест должен использовать моки; "
    "для живого hh.ru нужен маркер live_read, live_write или live_write_danger"
)


@pytest.fixture(autouse=True)
def _block_live_browser(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Не позволяет обычным тестам случайно выйти в браузер и сеть.

    Защита стоит на `PlaywrightContextManager.__enter__` — это фактическая точка
    старта движка: `sync_playwright()` лишь конструирует объект, а процесс
    Playwright поднимается только при входе в контекст-менеджер (`start()`
    внутри тоже вызывает `__enter__`). Патч метода НА КЛАССЕ перекрывает сразу
    все ссылки на фабрику, потому что объект создаётся позже, а метод ищется на
    классе в момент входа.

    Почему именно так, а не патчем имени `sync_playwright` по модулям: любая
    ссылка, захваченная до старта autouse-фикстуры, патч по имени переживает.
    Достаточно импорта под другим именем в шапке модуля теста
    (`from playwright.sync_api import sync_playwright as factory`), замыкания
    или default-аргумента — и заглушка обходится. Класс же один на всех.

    Дополнительно оставлен патч имени `sync_playwright` в загруженных модулях:
    он даёт раннюю и более внятную ошибку в обычном случае (`browser`, `auth`),
    не дожидаясь входа в контекст-менеджер. Это удобство, а не сама защита —
    инвариант держит `__enter__`.

    Тесту, подменившему `sync_playwright` своим моком (см. test_browser_navigation),
    фикстура не мешает: мок не является `PlaywrightContextManager`, поэтому
    исполняется только он сам.
    """
    if any(request.node.get_closest_marker(marker) for marker in _LIVE_MARKERS):
        return

    def _blocked_enter(self: PlaywrightContextManager) -> None:
        raise RuntimeError(_BLOCKED_MESSAGE)

    monkeypatch.setattr(PlaywrightContextManager, "__enter__", _blocked_enter)

    real_sync_playwright = playwright.sync_api.sync_playwright

    def _blocked_sync_playwright(*args: object, **kwargs: object):
        raise RuntimeError(_BLOCKED_MESSAGE)

    for module in tuple(sys.modules.values()):
        if module is None:
            continue
        if getattr(module, "sync_playwright", None) is real_sync_playwright:
            monkeypatch.setattr(module, "sync_playwright", _blocked_sync_playwright)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", _blocked_sync_playwright)


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

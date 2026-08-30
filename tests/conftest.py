"""Общие фикстуры и хелперы для characterization-тестов.

Тесты не запускают браузер — Playwright нужен только для импорта модулей,
вся тестируемая логика чистая (без Page/браузера).
"""

from __future__ import annotations

import hashlib
import sqlite3

import playwright._impl._driver
import playwright._impl._transport
import pytest
from playwright.sync_api._context_manager import PlaywrightContextManager

from hhru_bot import logging_setup
from hhru_bot.apply import probe
from hhru_bot.apply import steps as apply_steps
from hhru_bot.commands import log_cmd

_LIVE_MARKERS = ("live_read", "live_write", "live_write_danger", "browser_unit")

_BLOCKED_MESSAGE = (
    "Playwright заблокирован: тест должен использовать моки; "
    "для живого hh.ru нужен маркер live_read, live_write или live_write_danger"
)

# Модули, где `compute_driver_executable` доступна как атрибут: `_transport`
# импортирует её по имени, поэтому патчить надо оба, иначе останется живая ссылка.
_DRIVER_MODULES = (playwright._impl._driver, playwright._impl._transport)

_real_compute_driver_executable = playwright._impl._driver.compute_driver_executable
_real_sync_context_enter = PlaywrightContextManager.__enter__

# Снимается ровно на время исполнения теста с live-маркером (см. _allow_live_browser).
_live_allowed = False


def _guarded_compute_driver_executable() -> tuple[str, str]:
    if not _live_allowed:
        raise RuntimeError(_BLOCKED_MESSAGE)
    return _real_compute_driver_executable()


def _guarded_sync_context_enter(self):
    """Reject sync Playwright before its greenlet creates a background Future.

    Raising only from ``compute_driver_executable`` is a strong shared
    boundary, but Playwright's sync context manager records that exception in
    an internal asyncio Future when ``__enter__`` starts its dispatcher fiber.
    The Future is later reported as ``Future exception was never retrieved``.
    Keep the driver boundary below for async/direct entry points, while making
    the ordinary sync path fail before that noisy background task exists.
    """
    if not _live_allowed:
        raise RuntimeError(_BLOCKED_MESSAGE)
    return _real_sync_context_enter(self)


_real_sqlite_connect = sqlite3.connect


def _sqlite_connect_without_journal_file(*args, **kwargs):
    """SQLite без файла журнала: на него уходило ~40% времени прогона.

    Дефолтный `journal_mode=DELETE` создаёт и удаляет файл `-journal` на КАЖДУЮ
    транзакцию. Сьют открывает `History(tmp_path / ...)` ~690 раз и делает
    тысячи commit'ов, и на APFS это давало 25.7с system time из 82с wall
    (замер 2026-08-30). Журнал в памяти + `synchronous=OFF` убирают файловые
    операции: 82с → 64с однопоточно, system 25.7с → 8.2с — тот же движок, та
    же схема, тот же отдельный файл БД на тест (изоляция не меняется).

    Что при этом не теряется: ни один тест не проверяет устойчивость БД к
    обрыву питания — проверяется логика `history.py` (UNIQUE-индексы, lease,
    dedup), а она работает на той же реальной SQLite. Тесты обрыва процесса
    (`test_history_competitors`) убивают процесс между транзакциями, а не
    посреди записи, и на журнал не опираются.

    Ошибка PRAGMA глотается per-connection: read-only соединение (`mode=ro`,
    `commands/query.py`) может отказать, и это не повод ронять тест.
    """
    conn = _real_sqlite_connect(*args, **kwargs)
    try:
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
    except sqlite3.Error:
        pass
    return conn


def pytest_configure(config: pytest.Config) -> None:
    """Ставит защиту ДО сбора тестов (#217).

    Почему не autouse-фикстура: pytest импортирует все тест-модули на этапе
    collection — до применения `-m` и до запуска любой фикстуры. Код на уровне
    модуля исполняется прямо там, поэтому фикстура физически не успевает.
    Проверено: файл с маркером `live_write_danger`, который pytest ИСКЛЮЧИЛ из
    сбора (`1 deselected`), всё равно поднимал настоящий Chromium на импорте.

    Почему именно `compute_driver_executable`: это узкое место запуска
    процесса-драйвера — через него идут и sync, и async API, и прямое
    построение `PipeTransport`. Патч классов (`PlaywrightContextManager`)
    закрывал только sync-путь и обходился через `async_playwright`,
    `importlib.reload` или сохранённую до патча ссылку на метод. Граница
    процесса одна на все пути, поэтому держать инвариант надо на ней.

    Заглушка не снимается по завершении теста и не восстанавливается
    монки-патчем: единственный способ её обойти — маркер live_*, который
    открывает доступ только на время самого теста (`_allow_live_browser`).
    """
    for module in _DRIVER_MODULES:
        module.compute_driver_executable = _guarded_compute_driver_executable
    PlaywrightContextManager.__enter__ = _guarded_sync_context_enter
    # Под xdist pytest_configure выполняется в каждом воркере — патч доезжает
    # до всех процессов, где реально открываются соединения.
    sqlite3.connect = _sqlite_connect_without_journal_file


@pytest.fixture(autouse=True)
def _allow_live_browser(request: pytest.FixtureRequest) -> object:
    """Открывает доступ к движку только на время live-теста.

    Обычные тесты не трогают флаг вовсе — для них движок закрыт с момента
    `pytest_configure`. Флаг возвращается в исходное состояние в teardown, даже
    если тест упал, поэтому «открытость» не протекает на соседние тесты.
    """
    global _live_allowed
    if not any(request.node.get_closest_marker(marker) for marker in _LIVE_MARKERS):
        yield
        return

    previous = _live_allowed
    _live_allowed = True
    try:
        yield
    finally:
        _live_allowed = previous


@pytest.fixture(autouse=True)
def _isolate_log_dir(
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Страховка от записи тестов в реальный data/logs/hhru_bot.log (#131).

    До этой фикстуры изоляция держалась на дисциплине каждого отдельного теста
    (см. локальный monkeypatch в test_log_command_does_not_create_log, #129/#130) —
    не на инварианте. Здесь уводим LOG_DIR в свой каталог для ВСЕХ тестов сессии.

    DEFAULT_LOG_PATH (log_cmd.py), PROBE_LOG_DIR (apply/probe.py) и steps.LOG_DIR
    (apply/steps.py, дампы #195/#207) вычисляются/импортируются на импорте модуля
    как `LOG_DIR / ...` / `from ..logging_setup import LOG_DIR` и сами не
    пересчитаются при подмене LOG_DIR — патчим их отдельно тем же путём.

    Почему не `tmp_path` (#439): эта фикстура autouse, то есть выполняется для
    всех ~1500 тестов, а `tmp_path` СОЗДАЁТ каталог на диске при каждом запросе
    (~1.4 мс на тест — заметная доля прогона). Каталог логов при этом реально
    нужен единицам: остальным важно лишь, чтобы LOG_DIR не указывал на
    настоящий data/logs. Поэтому путь вычисляется от общей базы
    `tmp_path_factory`, но не создаётся — его создаст тот код, который в него
    действительно пишет. Тесты, которым нужен собственный каталог, как и
    раньше просят `tmp_path` явно.

    Ключ уникальности — sha1 полного `nodeid`, а не усечённое `node.name`:
    у параметризованных тестов имена длинные и различаются хвостом, поэтому
    обрезка давала бы коллизию, два теста делили бы каталог логов и протечка
    между тестами вернулась бы — ровно то, от чего фикстура защищает.

    Монки этой фикстуры и локальные monkeypatch внутри отдельных тестов
    накладываются безопасно (LIFO): более специфичный тестовый monkeypatch
    отменяется первым при teardown, эта фикстура — последней.
    """
    node_key = hashlib.sha1(request.node.nodeid.encode()).hexdigest()[:16]
    log_dir = tmp_path_factory.getbasetemp() / "isolated_logs" / node_key
    monkeypatch.setattr(logging_setup, "LOG_DIR", log_dir)
    monkeypatch.setattr(log_cmd, "DEFAULT_LOG_PATH", log_dir / "hhru_bot.log")
    monkeypatch.setattr(probe, "PROBE_LOG_DIR", log_dir)
    monkeypatch.setattr(apply_steps, "LOG_DIR", log_dir)

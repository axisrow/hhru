"""Runner миграций SQLite.

Миграции лежат в этом же пакете как .sql-файлы с числовым префиксом
(001_actions.sql, 002_responses.sql, ...). Применяются по порядку, один раз;
факт применения записывается в таблицу schema_migrations. Идемпотентно:
повторный запуск пропускает уже применённые миграции.

Конвенция нумерации: номер миграции = номер ишью, которое её вводит
(002 для #12, 017 для #17), чтобы параллельные воркеры не коллидировали в именах.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from importlib import resources

logger = logging.getLogger("hhru_bot.migrations")

_FILENAME_RE = re.compile(r"^(\d+)_.*\.sql$")


def _available_migrations() -> list[str]:
    """Возвращает имена .sql-миграций в пакете, отсортированные по числовому префиксу."""
    found: list[tuple[int, str]] = []
    for entry in resources.files(__package__).iterdir():
        match = _FILENAME_RE.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name))
    found.sort()
    return [name for _num, name in found]


def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return set()
    return {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Применяет все ещё не применённые миграции. Возвращает список имён применённых."""
    conn.executescript("CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY);")
    applied = _applied_migrations(conn)
    applied_now: list[str] = []
    for filename in _available_migrations():
        if filename in applied:
            continue
        sql = resources.files(__package__).joinpath(filename).read_text(encoding="utf-8")
        logger.debug("Применяю миграцию %s", filename)
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (filename,))
        applied_now.append(filename)
    if applied_now:
        logger.info("Применены миграции: %s", ", ".join(applied_now))
    return applied_now

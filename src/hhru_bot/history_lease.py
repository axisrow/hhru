"""Lease-хелперы supervised-команд: живость PID, grace-окно, время записи (#1035).

Выделено из ``history.py`` без изменения семантики (#479, #475).
``_row_is_live`` читает ``_pid_is_alive`` через фасад ``hhru_bot.history``
в момент вызова: тесты monkeypatch-атрибут фасада
(``hhru_bot.history._pid_is_alive``, см. test_bump_command_runs /
test_reliability_bundle), и позднее связывание сохраняет это контракт.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta


class CommandRunBusy(RuntimeError):
    """A live process already owns the supervised-command lease."""

    def __init__(self, run_id: str, command: str, owner_pid: int | None):
        self.run_id = run_id
        self.command = command
        self.owner_pid = owner_pid
        super().__init__(
            f"supervised-команда уже выполняется: command={command}, "
            f"pid={owner_pid}, run_id={run_id}"
        )


def _pid_is_alive(pid: int | None) -> bool:
    """Return whether a recorded local PID is confirmed alive.

    Legacy rows have no owner PID and are recoverable. Permission errors mean
    the process exists and therefore must be treated as alive (fail closed).
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


#: #479 (Codex adversarial-review of PR #478): ``owner_pid`` is a column added
#: by ``_ensure_column`` -- an idempotent ``ALTER TABLE`` that does not
#: backfill PIDs onto rows written by an older binary. A genuinely stale
#: legacy ``running`` row (created before this column existed, process long
#: gone) and a *live* one from an older binary still executing a supervised
#: command exactly across a ``git pull`` + reinstall between two terminal
#: sessions are both indistinguishable by ``owner_pid IS NULL`` alone. This
#: grace window trades a still-narrow reclaim delay for closing that overlap:
#: a NULL-owner row younger than the grace period is treated as live (blocks
#: a competing start, same as a confirmed-alive PID); older than it, it is
#: still reclaimed unconditionally -- a NULL-owner row from months ago is not
#: given an unbounded lease just because no PID was ever recorded. The window
#: is sized well above any single supervised command's realistic duration
#: (``apply``/``run`` under daily limits can run for a while), not "a couple
#: of minutes" -- comparable in order of magnitude to ``throttle.BUMP_COOLDOWN``.
LEGACY_LEASE_GRACE = timedelta(hours=6)


def _row_is_live(row: sqlite3.Row, *, now: datetime) -> bool:
    """Whether a ``running`` command_runs row still holds the lease (#479)."""
    owner_pid = row["owner_pid"]
    if owner_pid is not None:
        # Позднее связывание через фасад — см. докстринг модуля.
        from . import history as _facade

        return _facade._pid_is_alive(owner_pid)
    started_at = datetime.fromisoformat(row["started_at"])
    return started_at > now - LEGACY_LEASE_GRACE


def _parse_recorded_at(value: str) -> datetime:
    """Разобрать ``created_at`` из ``actions``, приведя его к локальному времени.

    Код пишет ``datetime.now().isoformat()`` — локальное время с разделителем
    ``'T'``. Но ручная reconciliation (CLAUDE.md, раздел 6) вставляет строку
    напрямую через SQL ``datetime('now')``, а SQLite отдаёт для него UTC с
    пробелом-разделителем. Naive-разбор такой строки выглядел бы старше на
    величину смещения таймзоны и обходил кулдаун (``bump`` — 4 часа), поэтому
    UTC-форма распознаётся по разделителю и переводится в локальное время.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    if " " in value:
        # Форма SQLite ``datetime('now')``: UTC без указания зоны. Проверяется
        # именно пробел-разделитель, а не отсутствие ``'T'``: под второе условие
        # попала бы и date-only строка (``date('now')``), для которой полночь
        # сдвинулась бы на величину смещения зоны вместо начала суток.
        return parsed.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)
    return parsed

"""Журнал supervised-запусков: lease ``command_runs`` и счётчики (#1035).

Выделено из ``history.py`` механически; семантика lease (owner_pid,
LEGACY_LEASE_GRACE) живёт в ``history_lease`` и ``History._init_schema``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

from .history_lease import CommandRunBusy, _row_is_live


class CommandsMixin:
    def start_command_run(self, *, command: str, requested_limit: int | None) -> str:
        """Recover dead owners and acquire the single supervised-command lease."""
        now = datetime.now().isoformat()
        run_id = str(uuid.uuid4())
        with self._connect() as conn:
            # Serialize the read/recover/insert sequence across processes.  A
            # normal deferred SQLite transaction would let two starters both
            # observe no owner before either INSERT commits.
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute(
                "SELECT run_id, command, owner_pid, started_at FROM command_runs "
                "WHERE status='running'"
            ).fetchall()
            for row in running:
                if _row_is_live(row, now=datetime.now()):
                    raise CommandRunBusy(row["run_id"], row["command"], row["owner_pid"])
            conn.execute(
                """UPDATE command_runs SET status='orphaned', finished_at=?, exit_code=NULL,
                          detail=COALESCE(detail, 'recovered after owner process exited')
                   WHERE status='running'""",
                (now,),
            )
            conn.execute(
                """INSERT INTO command_runs
                   (run_id, command, requested_limit, status, started_at, owner_pid)
                   VALUES (?, ?, ?, 'running', ?, ?)""",
                (run_id, command, requested_limit, now, os.getpid()),
            )
        return run_id

    def finish_command_run(
        self,
        run_id: str,
        *,
        status: str,
        exit_code: int,
        attempted: int,
        success: int,
        failed: int,
        uncertain: int,
        skipped: int,
        detail: str | None = None,
    ) -> None:
        allowed = {"completed", "partial", "failed", "interrupted", "orphaned"}
        if status not in allowed:
            raise ValueError(f"недопустимый статус command run: {status}")
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE command_runs SET status=?, attempted=?, success=?, failed=?,
                          uncertain=?, skipped=?, finished_at=?, exit_code=?, detail=?
                   WHERE run_id=? AND status='running'""",
                (
                    status,
                    attempted,
                    success,
                    failed,
                    uncertain,
                    skipped,
                    datetime.now().isoformat(),
                    exit_code,
                    detail,
                    run_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(f"running command run не найден: {run_id}")

    def record_selector_observations(
        self, run_id: str, observations: list[dict[str, object]]
    ) -> int:
        """Persist the confirmed selector observations for one healthcheck.

        The caller supplies logical IDs and already-classified statuses from
        ``commands.probe.SelectorCheck``.  Indeterminate page states have no
        selector rows and are ignored defensively here as well.  Catalog
        membership is checked by the producer because ``History`` is also
        used by offline callers that do not import the selector registry.
        """
        allowed_statuses = {"OK", "NOT_FOUND", "OPTIONAL_ABSENT"}
        from .selector_groups._generated import VALUES as selector_values

        rows: list[tuple[str, str, str, int, str]] = []
        for observation in observations:
            status = str(observation["status"])
            if status not in allowed_statuses:
                continue
            logical_id = str(observation["logical_id"])
            if logical_id not in selector_values:
                raise ValueError(f"unknown selector logical ID: {logical_id}")
            found = int(observation["found"])
            if found < 0:
                raise ValueError("selector observation count cannot be negative")
            rows.append(
                (
                    run_id,
                    logical_id,
                    status,
                    found,
                    str(observation.get("evidence", "")),
                )
            )
        with self._connect() as conn:
            if (
                conn.execute("SELECT 1 FROM command_runs WHERE run_id=?", (run_id,)).fetchone()
                is None
            ):
                raise ValueError(f"command run не найден: {run_id}")
            conn.executemany(
                """INSERT INTO selector_observations
                   (run_id, logical_id, status, found, evidence, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(*row, datetime.now().isoformat()) for row in rows],
            )
        return len(rows)

    def command_runs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM command_runs ORDER BY started_at")
            return [dict(row) for row in rows]

    def command_run_action_counts(self, run_id: str, *, action: str = "apply") -> dict[str, int]:
        """Return durable outcome counts for one action type in a command run."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT status, COUNT(*) AS count FROM actions
                   WHERE run_id=? AND action=? GROUP BY status""",
                (run_id, action),
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

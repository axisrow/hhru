from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .migrations import apply_migrations


class History:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._connect() as conn:
            apply_migrations(conn)

    def has_applied(self, resume_id: str, vacancy_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM actions
                WHERE resume_id = ? AND vacancy_id = ? AND action = 'apply'
                  AND status IN ('success', 'dry_run')
                LIMIT 1
                """,
                (resume_id, vacancy_id),
            ).fetchone()
            return row is not None

    def record_action(
        self,
        resume_id: str,
        vacancy_id: str,
        action: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO actions (resume_id, vacancy_id, action, status, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (resume_id, vacancy_id, action, status, reason, datetime.now().isoformat()),
            )

    def count_today(self, resume_id: str, action: str) -> int:
        today = datetime.now().date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM actions
                WHERE resume_id = ? AND action = ? AND status = 'success'
                  AND created_at >= ?
                """,
                (resume_id, action, today),
            ).fetchone()
            return row["cnt"] if row else 0

    def last_action_at(self, resume_id: str, action: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at FROM actions
                WHERE resume_id = ? AND action = ? AND status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (resume_id, action),
            ).fetchone()
            return datetime.fromisoformat(row["created_at"]) if row else None

    def time_since_last(self, resume_id: str, action: str) -> timedelta | None:
        last = self.last_action_at(resume_id, action)
        if last is None:
            return None
        return datetime.now() - last

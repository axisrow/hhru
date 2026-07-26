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

    # --- Агрегаты для команды stats (#11) -------------------------------------
    # Новые методы в конец файла: паттерн with self._connect(), существующие
    # методы не трогаем. summary/list_actions считают ВСЕ строки (success/
    # dry_run/failed) — для статистики нужен полный срез, а не только успех.

    _PERIOD_DAYS = {"week": 7, "month": 30}

    @staticmethod
    def _period_since(period: str) -> str | None:
        """ISO-отсечка created_at для периода. today = начало сегодняшнего дня,
        week/month = N дней назад, all = без отсечки (None)."""
        now = datetime.now()
        if period == "today":
            return now.date().isoformat()
        days = History._PERIOD_DAYS.get(period)
        if days is not None:
            return (now - timedelta(days=days)).isoformat()
        return None  # all

    def summary(self, resume_id: str | None, period: str) -> dict:
        """Срез счётчиков action × status за период.

        Возвращает {"apply": {"success","dry_run","failed"}, "bump": {...}, "total"}.
        Пустой период → все нули. resume_id=None означает «по всем резюме».
        """
        result: dict = {
            "apply": {"success": 0, "dry_run": 0, "failed": 0},
            "bump": {"success": 0, "dry_run": 0, "failed": 0},
            "total": 0,
        }
        where = []
        params: list = []
        if resume_id is not None:
            where.append("resume_id = ?")
            params.append(resume_id)
        since = self._period_since(period)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT action, status, COUNT(*) AS cnt FROM actions{clause} "
                "GROUP BY action, status",
                params,
            ).fetchall()
        for row in rows:
            action = row["action"]
            status = row["status"]
            cnt = row["cnt"]
            if action in result and status in result[action]:
                result[action][status] = cnt
            result["total"] += cnt
        return result

    def list_actions(
        self, resume_id: str | None, period: str, limit: int = 50
    ) -> list[dict]:
        """Последние действия (свежие первыми) для таблицы stats.

        Возвращает список словарей с ключами resume_id/vacancy_id/action/status/
        reason/created_at. resume_id=None — по всем резюме.
        """
        where = []
        params: list = []
        if resume_id is not None:
            where.append("resume_id = ?")
            params.append(resume_id)
        since = self._period_since(period)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT resume_id, vacancy_id, action, status, reason, created_at "
                f"FROM actions{clause} ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

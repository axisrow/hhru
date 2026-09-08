"""Очередь review (#1035): ручное подтверждение писем перед отправкой.

Выделено из ``history.py`` механически; транзакционные границы не менялись
(каждый метод по-прежнему открывает собственное соединение через
``History._connect``).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta


class ReviewQueueMixin:
    def enqueue_review(
        self, resume_id, card, score, breakdown, letter, *, search_query: str | None = None
    ) -> int:
        """Store the exact dry-run candidate and letter for later approval.

        ``search_query`` (#420 follow-up, PR #449 Codex adversarial-review) is the
        query the card was found under at enqueue time — persisted so a later
        `apply --approved` attributes the resulting action to it, not to whatever
        the config's search text says by the time it's approved (config drift).
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO review_queue
                (resume_id,vacancy_id,vacancy_url,title,company,score,breakdown,letter,
                 search_query,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    resume_id,
                    card.vacancy_id,
                    card.url,
                    card.title,
                    card.company,
                    score,
                    json.dumps(breakdown, sort_keys=True),
                    letter,
                    search_query,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def review_items(self, status=None):
        with self._connect() as conn:
            query = "SELECT * FROM review_queue"
            params = ()
            if status:
                query += " WHERE status = ?"
                params = (status,)
            query += " ORDER BY id"
            return [dict(row) for row in conn.execute(query, params)]

    def edit_review_letter(self, item_id: int, letter: str) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE review_queue SET letter=?, updated_at=? WHERE id=? AND status='pending'",
                (letter, datetime.now().isoformat(), item_id),
            )
            if cur.rowcount != 1:
                raise ValueError("запись очереди не найдена или уже обработана")

    def approve_review(self, item_id: int, ttl_seconds: int = 900) -> str:
        permit = secrets.token_urlsafe(32)
        now = datetime.now()
        expires = now + timedelta(seconds=ttl_seconds)
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE review_queue SET status='approved', permit_hash=?,
                   permit_expires_at=?, updated_at=?
                   WHERE id=? AND status='pending'""",
                (
                    hashlib.sha256(permit.encode()).hexdigest(),
                    expires.isoformat(),
                    now.isoformat(),
                    item_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("запись очереди не найдена или уже обработана")
        return permit

    def claim_review(self, item_id: int, permit: str | None = None) -> dict:
        """Atomically claim an approved item; expired permits cannot run."""
        now = datetime.now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT permit_hash FROM review_queue WHERE id=?", (item_id,)
            ).fetchone()
            if (
                row is None
                or permit is None
                or not secrets.compare_digest(
                    row[0] or "", hashlib.sha256(permit.encode()).hexdigest()
                )
            ):
                raise ValueError("неверный permit")
            cur = conn.execute(
                """UPDATE review_queue SET status='applying', updated_at=?
                   WHERE id=? AND status='approved' AND permit_expires_at > ?""",
                (now.isoformat(), item_id, now.isoformat()),
            )
            if cur.rowcount != 1:
                raise ValueError("запись не approved или её permit истёк")
            row = conn.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
            return dict(row)

    def finish_review(self, item_id: int, status: str) -> None:
        if status not in {"applied", "failed", "skipped"}:
            raise ValueError(f"недопустимый статус очереди: {status}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE review_queue SET status=?, updated_at=? WHERE id=?",
                (status, datetime.now().isoformat(), item_id),
            )

    def requeue_review(self, item_id: int) -> None:
        """Return a confirmed pre-submit failure to pending, never a possible submit."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT resume_id, vacancy_id, status FROM review_queue WHERE id=?", (item_id,)
            ).fetchone()
            if row is None or row["status"] != "failed":
                raise ValueError("повторно поставить можно только failed-запись")
            unsafe = conn.execute(
                """SELECT status FROM actions
                   WHERE resume_id=? AND vacancy_id=? AND action='apply'
                     AND status IN ('success', 'uncertain')
                   ORDER BY id DESC LIMIT 1""",
                (row["resume_id"], row["vacancy_id"]),
            ).fetchone()
            if unsafe is not None:
                raise ValueError(
                    f"безопасный повтор запрещён: action имеет статус {unsafe['status']}"
                )
            conn.execute(
                """UPDATE review_queue
                   SET status='pending', permit_hash=NULL, permit_expires_at=NULL, updated_at=?
                   WHERE id=? AND status='failed'""",
                (datetime.now().isoformat(), item_id),
            )

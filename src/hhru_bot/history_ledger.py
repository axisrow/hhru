"""Журнал действий (actions): запись, дедупликация, лимиты, uncertain (#1035).

Выделено из ``history.py`` механически. Дедупликация опирается на частичный
UNIQUE-индекс idx_resume_vacancy_apply (см. ``history_schema``); попытка
засчитывается ровно один раз (эпик #459).
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta

from .history_lease import _parse_recorded_at


class LedgerMixin:
    def has_applied(self, resume_id: str, vacancy_id: str) -> bool:
        # #176: 'uncertain' (submit мог уйти, Playwright упал в момент клика)
        # тоже дедуплицируется: неопределённый отклик обязан отсекать вакансию
        # от повторного отклика — это дешевле, чем второе письмо работодателю.
        # Обычный 'failed' (клик был, успеха не подтвердили) дедупликацией
        # остаётся НЕ виден — как раньше.
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM actions
                WHERE resume_id = ? AND vacancy_id = ? AND action = 'apply'
                  AND status IN ('success', 'uncertain')
                LIMIT 1
                """,
                (resume_id, vacancy_id),
            ).fetchone()
            if row is not None:
                return True
            return (
                conn.execute(
                    "SELECT 1 FROM external_applied WHERE resume_id=? AND vacancy_id=? LIMIT 1",
                    (resume_id, vacancy_id),
                ).fetchone()
                is not None
            )

    def sync_external_applied(self, cards) -> dict[str, int]:
        """Import only unambiguous negotiation mappings; never delete markers."""
        now = datetime.now().isoformat()
        imported = ambiguous = skipped = 0
        invalid = []
        for card in cards:
            if getattr(card, "topic_ambiguous", False) or not card.topic or not card.resume_id:
                invalid.append(card.vacancy_id)
        if invalid:
            raise ValueError(
                "external application sync is indeterminate: "
                f"{len(invalid)} negotiation card(s) lack unambiguous SSR attribution"
            )
        with self._connect() as conn:
            for card in cards:
                if getattr(card, "topic_ambiguous", False):
                    ambiguous += 1
                    continue
                if not card.topic or not card.resume_id:
                    skipped += 1
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM external_applied WHERE resume_id=? AND vacancy_id=? AND topic=?",
                    (card.resume_id, card.vacancy_id, card.topic),
                ).fetchone()
                conn.execute(
                    """INSERT INTO external_applied
                       (resume_id,vacancy_id,topic,origin,first_seen_at,last_seen_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(resume_id,vacancy_id,topic)
                       DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                    (card.resume_id, card.vacancy_id, card.topic, "manual_external", now, now),
                )
                if exists is None:
                    imported += 1
        return {"imported": imported, "ambiguous": ambiguous, "skipped": skipped}

    def record_resume_views(self, rows: list[dict]) -> int:
        """Persist real employer-view snapshots and return newly inserted count."""
        if not rows:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        inserted = 0
        with self._connect() as conn:
            for row in rows:
                # view_key: the SSR per-view source_id when known, else
                # employer_id, else '' — never the mutable `employer` display
                # string on its own (#428 review).
                view_key = row.get("source_id") or row.get("employer_id") or ""
                cur = conn.execute(
                    """INSERT OR IGNORE INTO resume_views
                       (resume_id, employer_id, employer, view_key, viewed_at, first_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(row["resume_id"]),
                        row.get("employer_id") or "",
                        row.get("employer") or "",
                        str(view_key),
                        str(row["viewed_at"]),
                        now,
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def resume_views(self, resume_id: str | None = None) -> list[dict]:
        """Return stored employer-view snapshots, newest first."""
        with self._connect() as conn:
            query = "SELECT * FROM resume_views"
            params: tuple = ()
            if resume_id is not None:
                query += " WHERE resume_id = ?"
                params = (resume_id,)
            rows = conn.execute(query + " ORDER BY viewed_at DESC, id DESC", params).fetchall()
        return [dict(row) for row in rows]

    def last_action_status(
        self,
        resume_id: str,
        vacancy_id: str,
        action: str,
        *,
        statuses: tuple[str, ...] = ("success", "uncertain"),
    ) -> str | None:
        """Return the latest status if it is one of the deduplicating statuses.

        Callers use this as a fail-closed guard before repeating an external
        mutation: ``uncertain`` must block a retry, since the browser may have
        completed the action even when confirmation failed. A later ordinary
        ``failed`` row means the earlier uncertain action was resolved before
        the retry, so it must not keep blocking a new attempt.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status FROM actions
                WHERE resume_id = ? AND vacancy_id = ? AND action = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (resume_id, vacancy_id, action),
            ).fetchone()
            if row is None or row[0] not in statuses:
                return None
            return row[0]

    def record_action(
        self,
        resume_id: str | None,
        vacancy_id: str,
        action: str,
        status: str,
        reason: str | None = None,
        letter_variant: str | None = None,
        search_query: str | None = None,
        run_id: str | None = None,
        reason_code: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO actions
                    (
                        resume_id, vacancy_id, action, status, reason,
                        letter_variant, search_query, run_id, reason_code, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resume_id,
                    vacancy_id,
                    action,
                    status,
                    reason,
                    letter_variant,
                    search_query,
                    run_id,
                    reason_code,
                    datetime.now().isoformat(),
                ),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    @classmethod
    def _feedback_snippet(cls, generated: str | None, edited: str | None) -> str | None:
        """Return a bounded, redacted diff instead of storing whole letters."""
        if generated is None or edited is None or generated == edited:
            return None
        generated = generated[: cls.FEEDBACK_LETTER_MAX]
        edited = edited[: cls.FEEDBACK_LETTER_MAX]
        matcher = difflib.SequenceMatcher(a=generated, b=edited, autojunk=False)
        chunks = []
        for tag, a1, a2, b1, b2 in matcher.get_opcodes():
            if tag != "equal":
                chunks.append(f"-{generated[a1:a2]}\n+{edited[b1:b2]}")
        snippet = "\n".join(chunks)
        # Do not persist common direct identifiers from manually edited letters.
        snippet = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", snippet)
        snippet = re.sub(r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)", "[redacted-phone]", snippet)
        return snippet[: cls.FEEDBACK_SNIPPET_MAX] or None

    def record_reject(
        self,
        resume_id: str,
        vacancy_id: str,
        reason: str,
        *,
        generated_letter: str | None = None,
        edited_letter: str | None = None,
    ) -> int:
        """Record one explicit manual rejection and an optional letter diff."""
        reason = " ".join(reason.split()).strip()
        if not reason:
            raise ValueError("Причина отклонения не может быть пустой")
        reason = reason[: self.FEEDBACK_REASON_MAX]
        snippet = self._feedback_snippet(generated_letter, edited_letter)
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO vacancy_feedback
                   (resume_id, vacancy_id, action, reason, edited_snippet, created_at)
                   VALUES (?, ?, 'reject', ?, ?, ?)""",
                (resume_id, vacancy_id, reason, snippet, now),
            )
            # Keep the action visible to existing action/history consumers while
            # retaining the letter-specific payload in vacancy_feedback.
            conn.execute(
                """INSERT INTO actions
                   (resume_id, vacancy_id, action, status, reason, letter_variant, created_at)
                   VALUES (?, ?, 'reject', 'success', ?, NULL, ?)""",
                (resume_id, vacancy_id, reason, now),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def list_feedback(self, *, resume_id: str | None = None, limit: int = 20) -> list[dict]:
        """Return newest manual feedback rows for future prompt consumers."""
        query = "SELECT * FROM vacancy_feedback"
        params: list[object] = []
        if resume_id is not None:
            query += " WHERE resume_id = ?"
            params.append(resume_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def begin_action(
        self,
        resume_id: str,
        vacancy_id: str,
        action: str,
        *,
        search_query: str | None = None,
        run_id: str | None = None,
    ) -> int:
        """Durably reserve a potentially external action before browser work.

        ``uncertain`` is deliberate: if the process disappears after the browser
        side effect but before its result is recorded, ``has_applied`` must still
        block a duplicate on the next run.  A normal completion changes this row
        in place via :meth:`finalize_action`.
        """
        return self.record_action(
            resume_id,
            vacancy_id,
            action,
            "uncertain",
            reason="действие начато, результат не зафиксирован",
            search_query=search_query,
            run_id=run_id,
            reason_code="started",
        )

    def finalize_action(
        self,
        action_id: int,
        status: str,
        reason: str | None = None,
        letter_variant: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        """Finalize a pre-action audit marker without creating a second row.

        cycle-review PR #460 (round 3, Claude /review): ``reason_code`` was
        written unconditionally, so a caller that omits the new kwarg (there
        are several, e.g. the skip and AntiBotChallengeDetected paths in
        ``commands/_common.py``) silently overwrote ``begin_action``'s
        ``"started"`` marker with NULL, erasing the audit trail this column
        exists for. ``COALESCE`` keeps the existing value when a caller
        passes ``None`` — same pattern already used for ``resume_id`` in
        ``upsert_response`` (#1119) — an explicit caller that wants to clear
        it can still pass an empty string.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE actions
                   SET status = ?, reason = ?, letter_variant = ?,
                       reason_code = COALESCE(?, reason_code)
                 WHERE id = ?
                """,
                (status, reason, letter_variant, reason_code, action_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Действие истории не найдено: id={action_id}")

    def count_today(self, resume_id: str, action: str) -> int:
        # #176: 'uncertain' расходует дневной лимит — действие могло выполниться
        # на hh.ru, fail-closed считает его состоявшимся (dry_run/failed — нет).
        # Пустой resume_id — account-wide sentinel (так replies не привязаны к
        # конкретному резюме). Для apply это также важно: дневной лимит
        # относится к аккаунту, даже если действия в истории привязаны к
        # отдельным резюме.
        today = datetime.now().date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM actions
                WHERE (? = '' OR resume_id = ?) AND action = ?
                  AND status IN ('success', 'uncertain')
                  AND created_at >= ?
                """,
                (resume_id, resume_id, action, today),
            ).fetchone()
            return row["cnt"] if row else 0

    def last_action_at(self, resume_id: str, action: str) -> datetime | None:
        # #176: 'uncertain' запускает кулдаун (can_bump_now 4ч) — поднятие могло
        # выполниться; 'dry_run'/'failed' кулдаун не запускают, как раньше.
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at FROM actions
                WHERE resume_id = ? AND action = ? AND status IN ('success', 'uncertain')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (resume_id, action),
            ).fetchone()
            return _parse_recorded_at(row["created_at"]) if row else None

    def time_since_last(self, resume_id: str, action: str) -> timedelta | None:
        last = self.last_action_at(resume_id, action)
        if last is None:
            return None
        return datetime.now() - last

    def has_unresolved_uncertain(self, resume_id: str, action: str) -> bool:
        """Whether an 'uncertain' row for ``resume_id``/``action`` is unresolved.

        Unresolved means: an 'uncertain' row exists with no later 'success' row
        for the same resume_id/action. Checking only the single most-recent row
        (as opposed to this "any uncertain since the last success" scan) would
        let an intervening 'failed' row — e.g. a NotAuthenticated retry that
        never reached the click — silently clear an earlier unresolved
        uncertain, since 'failed' means "never attempted the click", not
        "the earlier uncertain was resolved".
        """
        with self._connect() as conn:
            # Порядок берётся по ``id`` (монотонный rowid), а НЕ по ``created_at``.
            # Форматы ``created_at`` в таблице смешаны: код пишет
            # ``datetime.now().isoformat()`` (локальное время, разделитель 'T'),
            # а ручная reconciliation (CLAUDE.md, раздел 6) — SQL
            # ``datetime('now')`` (UTC, разделитель ' '). Строковое сравнение
            # ``created_at > ?`` считало документированную резолюцию НЕ более
            # поздней (0x20 < 0x54) и не снимало блокировку никогда; сравнение
            # же разобранными датами спотыкалось о секундную точность
            # ``datetime('now')``. ``id`` свободен от обеих проблем: он отражает
            # фактический порядок вставки и не зависит от формата и таймзоны.
            last_success = conn.execute(
                """
                SELECT MAX(id) AS id FROM actions
                WHERE resume_id = ? AND action = ? AND status = 'success'
                """,
                (resume_id, action),
            ).fetchone()
            params: tuple = (resume_id, action)
            since_clause = ""
            if last_success and last_success["id"] is not None:
                since_clause = "AND id > ?"
                params = (*params, last_success["id"])
            row = conn.execute(
                f"""
                SELECT 1 FROM actions
                WHERE resume_id = ? AND action = ? AND status = 'uncertain' {since_clause}
                LIMIT 1
                """,
                params,
            ).fetchone()
            return row is not None

    def list_unresolved_uncertain(self, limit: int = 50) -> list[dict]:
        """Return the operator queue, derived directly from the action ledger."""
        if limit < 1:
            raise ValueError("limit должен быть >= 1")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT a.*, r.command, r.started_at AS run_started_at
                   FROM actions a LEFT JOIN command_runs r ON r.run_id=a.run_id
                   WHERE a.status='uncertain'
                     AND NOT EXISTS (
                       SELECT 1 FROM actions later
                       WHERE later.resume_id=a.resume_id AND later.vacancy_id=a.vacancy_id
                         AND later.action=a.action AND later.id>a.id
                         AND later.status='success' AND a.action != 'reply'
                     )
                   ORDER BY a.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_uncertain(self, action_id: int) -> dict | None:
        """Return one unresolved uncertain action, or ``None``."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT a.*, r.command, r.started_at AS run_started_at
                   FROM actions a LEFT JOIN command_runs r ON r.run_id=a.run_id
                   WHERE a.id=? AND a.status='uncertain'
                     AND NOT EXISTS (
                       SELECT 1 FROM actions later
                       WHERE later.resume_id=a.resume_id AND later.vacancy_id=a.vacancy_id
                         AND later.action=a.action AND later.id>a.id
                         AND later.status='success' AND a.action != 'reply'
                     )""",
                (action_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def reconcile_uncertain(self, action_id: int, status: str, evidence: str) -> None:
        """Close a queue row only after a verifier supplied authoritative evidence."""
        if status not in {"success", "failed"} or not evidence.strip():
            raise ValueError("reconcile требует status success/failed и evidence")
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE actions SET status=?, reason=?
                   WHERE id=? AND status='uncertain'""",
                (status, evidence, action_id),
            )
            if cur.rowcount != 1:
                raise ValueError("uncertain-запись уже закрыта или не найдена")

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
        days = LedgerMixin._PERIOD_DAYS.get(period)
        if days is not None:
            return (now - timedelta(days=days)).isoformat()
        return None  # all

"""Сбор конкурентов: durable collection runs и снимки резюме (#1035).

Выделено из ``history.py`` механически; lease/heartbeat-семантика
``competitor_collection_runs`` не менялась.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from .history_lease import CommandRunBusy, _row_is_live
from .history_schema import LEGACY_UNKNOWN_SCOPE


class CompetitorsMixin:
    def begin_competitor_collection(
        self,
        search_query: str,
        max_pages: int,
        *,
        requested_page_size: int = 100,
        auth_mode: str = "anonymous",
        search_in: str = "position",
        resume: bool = False,
    ) -> dict:
        """Recover dead collectors and atomically create a durable owned run (#654)."""
        now_dt = datetime.now()
        now = now_dt.isoformat(timespec="seconds")
        run_id = str(uuid.uuid4())
        recovered: list[dict] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute(
                """SELECT run_id, search_query, owner_pid, started_at, heartbeat_at,
                          pages_fetched, cards_seen, details_saved, details_failed,
                          last_started_page, last_completed_page, resume_page
                   FROM competitor_collection_runs WHERE status='running'"""
            ).fetchall()
            for row in running:
                if _row_is_live(row, now=now_dt):
                    raise CommandRunBusy(row["run_id"], "competitors collect", row["owner_pid"])
                had_progress = bool(
                    row["pages_fetched"]
                    or row["cards_seen"]
                    or row["details_saved"]
                    or row["details_failed"]
                    or row["last_started_page"] is not None
                )
                status = "partial" if had_progress else "failed"
                heartbeat = row["heartbeat_at"] or "отсутствует"
                detail = (
                    "owner process exited without finalization; "
                    f"owner_pid={row['owner_pid']}; last_heartbeat={heartbeat}"
                )
                conn.execute(
                    """UPDATE competitor_collection_runs
                       SET status=?, finished_at=?, exit_code=NULL, detail=?
                       WHERE run_id=? AND status='running'""",
                    (status, now, detail[:1000], row["run_id"]),
                )
                recovered.append({**dict(row), "status": status, "detail": detail})

            checkpoint = None
            if resume:
                # NULL в search_in — прогон, записанный до #669, когда `pos`
                # был жёстко full_text: COALESCE описывает факт, а не догадку.
                # Как следствие, `--resume` под новым дефолтом `position` такой
                # чекпоинт не подхватит и начнёт с нуля — это правильный
                # исход, а не потеря: 619 результатов против ~5000 означают
                # несопоставимую нумерацию страниц, и продолжение с чужого
                # смещения молча пропустило бы бо'льшую часть узкой выборки.
                latest = conn.execute(
                    """SELECT * FROM competitor_collection_runs
                       WHERE search_query=? AND requested_page_size=? AND auth_mode=?
                         AND COALESCE(search_in, 'full_text')=?
                         AND status != 'running'
                       ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                    (search_query, requested_page_size, auth_mode, search_in),
                ).fetchone()
                if (
                    latest is not None
                    and latest["status"] in {"partial", "failed", "limited"}
                    and latest["resume_page"] is not None
                ):
                    checkpoint = latest
            resume_page = int(checkpoint["resume_page"]) if checkpoint is not None else 0
            resumed_from = checkpoint["run_id"] if checkpoint is not None else None
            resume_observed_page_size = (
                int(checkpoint["observed_page_size"])
                if checkpoint is not None and checkpoint["observed_page_size"]
                else None
            )
            resume_rank_offset = 0
            rank_checkpoint = checkpoint
            seen_run_ids: set[str] = set()
            while rank_checkpoint is not None and rank_checkpoint["run_id"] not in seen_run_ids:
                seen_run_ids.add(rank_checkpoint["run_id"])
                # #660 (Codex review): cards_seen includes the in-progress
                # page's cards as soon as they're parsed, before all of that
                # page's details are fetched -- but resume_page still points
                # at that same unfinished page, which gets re-parsed from
                # scratch on resume. Using cards_seen verbatim here would
                # double-count that page's cards into the rank offset.
                # cards_seen_completed tracks cards from *completed* pages
                # only and is the correct offset source; legacy rows (NULL,
                # predating this column) fall back to cards_seen unchanged.
                completed = rank_checkpoint["cards_seen_completed"]
                resume_rank_offset += int(
                    completed if completed is not None else (rank_checkpoint["cards_seen"] or 0)
                )
                previous_run_id = rank_checkpoint["resumed_from_run_id"]
                if not previous_run_id:
                    break
                rank_checkpoint = conn.execute(
                    "SELECT * FROM competitor_collection_runs WHERE run_id=?",
                    (previous_run_id,),
                ).fetchone()
            conn.execute(
                """INSERT INTO competitor_collection_runs
                   (run_id, search_query, auth_mode, search_in, max_pages,
                    requested_page_size, status,
                    started_at, heartbeat_at,
                    owner_pid, last_started_page, last_completed_page, resume_page,
                    resumed_from_run_id, observed_page_size)
                   VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, NULL, ?, ?, ?)""",
                (
                    run_id,
                    search_query,
                    auth_mode,
                    search_in,
                    max_pages,
                    requested_page_size,
                    now,
                    now,
                    os.getpid(),
                    resume_page,
                    resumed_from,
                    resume_observed_page_size,
                ),
            )
        return {
            "run_id": run_id,
            "resume_page": resume_page,
            "resume_rank_offset": resume_rank_offset,
            "resume_observed_page_size": resume_observed_page_size,
            "resumed_from_run_id": resumed_from,
            "recovered": recovered,
        }

    def start_competitor_collection(
        self,
        search_query: str,
        max_pages: int,
        *,
        requested_page_size: int = 100,
        auth_mode: str = "anonymous",
    ) -> str:
        """Compatibility wrapper for a fresh durable competitor run."""
        return self.begin_competitor_collection(
            search_query,
            max_pages,
            requested_page_size=requested_page_size,
            auth_mode=auth_mode,
        )["run_id"]

    def checkpoint_competitor_collection(
        self,
        run_id: str,
        *,
        pages_fetched: int,
        cards_seen: int,
        details_saved: int,
        details_failed: int,
        last_started_page: int | None,
        last_completed_page: int | None,
        resume_page: int | None,
        observed_page_size: int | None,
        cards_seen_completed: int | None = None,
    ) -> None:
        """Persist one heartbeat/checkpoint while the collector still owns the run."""
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE competitor_collection_runs
                   SET pages_fetched=?, cards_seen=?, details_saved=?, details_failed=?,
                       last_started_page=?, last_completed_page=?, resume_page=?,
                       observed_page_size=?, cards_seen_completed=?, heartbeat_at=?
                   WHERE run_id=? AND status='running' AND owner_pid=?""",
                (
                    pages_fetched,
                    cards_seen,
                    details_saved,
                    details_failed,
                    last_started_page,
                    last_completed_page,
                    resume_page,
                    observed_page_size,
                    cards_seen_completed,
                    datetime.now().isoformat(timespec="seconds"),
                    run_id,
                    os.getpid(),
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(f"running competitor collection не найден: {run_id}")

    def finish_competitor_collection(
        self,
        run_id: str,
        *,
        status: str,
        pages_fetched: int,
        cards_seen: int,
        details_saved: int,
        details_failed: int,
        detail: str | None = None,
        exit_code: int | None = None,
        resume_page: int | None = None,
        last_started_page: int | None = None,
        last_completed_page: int | None = None,
        observed_page_size: int | None = None,
        cards_seen_completed: int | None = None,
    ) -> None:
        allowed = {"complete", "limited", "partial", "failed"}
        if status not in allowed:
            raise ValueError(f"недопустимый статус competitor collection: {status}")
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE competitor_collection_runs
                   SET status = ?, pages_fetched = ?, cards_seen = ?, details_saved = ?,
                       details_failed = ?, finished_at = ?, detail = ?, exit_code = ?,
                       resume_page = ?, last_started_page = ?, last_completed_page = ?,
                       observed_page_size = ?, cards_seen_completed = ?, heartbeat_at = ?
                   WHERE run_id = ? AND status = 'running' AND owner_pid = ?""",
                (
                    status,
                    pages_fetched,
                    cards_seen,
                    details_saved,
                    details_failed,
                    now,
                    detail,
                    exit_code,
                    resume_page,
                    last_started_page,
                    last_completed_page,
                    observed_page_size,
                    cards_seen_completed,
                    now,
                    run_id,
                    os.getpid(),
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(f"running competitor collection не найден: {run_id}")

    def competitor_collection_runs(self, search_query: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if search_query is None:
                rows = conn.execute(
                    "SELECT * FROM competitor_collection_runs ORDER BY started_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM competitor_collection_runs
                       WHERE search_query=? ORDER BY started_at""",
                    (search_query,),
                ).fetchall()
        return [dict(row) for row in rows]

    def upsert_competitor_resume(
        self,
        snapshot: dict,
        *,
        search_query: str,
        search_rank: int,
        search_in: str = "full_text",
        auth_mode: str = LEGACY_UNKNOWN_SCOPE,
    ) -> str:
        """Atomically replace one confirmed current snapshot and its skills."""
        now = datetime.now().isoformat(timespec="seconds")
        resume_id = str(snapshot["resume_id"])
        content_hash = str(snapshot["content_hash"])
        json_fields = (
            "specializations",
            "employment_types",
            "work_formats",
            "languages",
            "education",
        )
        encoded = {
            field: json.dumps(snapshot.get(field) or [], ensure_ascii=False, sort_keys=True)
            for field in json_fields
        }

        with self._connect() as conn:
            previous = conn.execute(
                "SELECT content_hash FROM competitor_resumes WHERE resume_id = ?", (resume_id,)
            ).fetchone()
            outcome = (
                "new"
                if previous is None
                else "unchanged"
                if previous["content_hash"] == content_hash
                else "updated"
            )
            conn.execute(
                """INSERT INTO competitor_resumes
                   (resume_id, resume_url, desired_role, area, relocation,
                    business_trips, metro_station, salary_from, salary_to,
                    salary_currency, experience_months, specializations, employment_types,
                    work_formats, languages, education, experience_summary, achievements,
                    content_hash, first_seen_at, last_seen_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(resume_id) DO UPDATE SET
                     resume_url = excluded.resume_url,
                     desired_role = excluded.desired_role,
                     area = excluded.area,
                     relocation = excluded.relocation,
                     business_trips = excluded.business_trips,
                     metro_station = excluded.metro_station,
                     salary_from = excluded.salary_from,
                     salary_to = excluded.salary_to,
                     salary_currency = excluded.salary_currency,
                     experience_months = excluded.experience_months,
                     specializations = excluded.specializations,
                     employment_types = excluded.employment_types,
                     work_formats = excluded.work_formats,
                     languages = excluded.languages,
                     education = excluded.education,
                     experience_summary = excluded.experience_summary,
                     achievements = excluded.achievements,
                     content_hash = excluded.content_hash,
                     last_seen_at = excluded.last_seen_at,
                     updated_at = CASE
                       WHEN competitor_resumes.content_hash <> excluded.content_hash
                       THEN excluded.updated_at ELSE competitor_resumes.updated_at END""",
                (
                    resume_id,
                    snapshot["resume_url"],
                    snapshot["desired_role"],
                    snapshot.get("area"),
                    snapshot.get("relocation"),
                    snapshot.get("business_trips"),
                    snapshot.get("metro_station"),
                    snapshot.get("salary_from"),
                    snapshot.get("salary_to"),
                    snapshot.get("salary_currency"),
                    snapshot.get("experience_months"),
                    encoded["specializations"],
                    encoded["employment_types"],
                    encoded["work_formats"],
                    encoded["languages"],
                    encoded["education"],
                    snapshot.get("experience_summary"),
                    snapshot.get("achievements"),
                    content_hash,
                    now,
                    now,
                    now,
                ),
            )

            old_skills = {
                row["skill"]: row["first_seen_at"]
                for row in conn.execute(
                    "SELECT skill, first_seen_at FROM competitor_resume_skills WHERE resume_id = ?",
                    (resume_id,),
                )
            }
            conn.execute("DELETE FROM competitor_resume_skills WHERE resume_id = ?", (resume_id,))
            for skill in snapshot.get("skills") or []:
                name = str(skill["name"]).strip()
                if not name:
                    continue
                conn.execute(
                    """INSERT INTO competitor_resume_skills
                       (resume_id, skill, proficiency, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (resume_id, name, skill.get("proficiency"), old_skills.get(name, now), now),
                )

            conn.execute(
                """INSERT INTO competitor_resume_queries
                   (resume_id, search_query, search_in, auth_mode,
                    search_rank, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(resume_id, search_query, search_in, auth_mode) DO UPDATE SET
                     search_rank = excluded.search_rank,
                     last_seen_at = excluded.last_seen_at""",
                (resume_id, search_query, search_in, auth_mode, search_rank, now, now),
            )
        return outcome

    def list_competitor_resumes(
        self,
        search_query: str | None = None,
        *,
        search_in: str | None = None,
        auth_mode: str | None = None,
    ) -> list[dict]:
        """Return current snapshots with source-faithful skills, optionally scoped by query.

        ``search_in``/``auth_mode`` narrow the membership to one collected
        population (#669): the same ``--text`` under a different scope is a
        different result set, not a refinement of the same one. ``None`` keeps
        the previous behaviour and spans every scope.
        """
        with self._connect() as conn:
            if search_query is None:
                rows = conn.execute(
                    "SELECT r.* FROM competitor_resumes r ORDER BY r.last_seen_at DESC, r.resume_id"
                ).fetchall()
            else:
                conditions = ["q.search_query = ?"]
                params: list[str] = [search_query]
                if search_in is not None:
                    conditions.append("q.search_in = ?")
                    params.append(search_in)
                if auth_mode is not None:
                    conditions.append("q.auth_mode = ?")
                    params.append(auth_mode)
                rows = conn.execute(
                    f"""SELECT r.*, MIN(q.search_rank) AS scope_rank
                       FROM competitor_resumes r
                       JOIN competitor_resume_queries q ON q.resume_id = r.resume_id
                       WHERE {" AND ".join(conditions)}
                       GROUP BY r.resume_id
                       ORDER BY scope_rank, r.resume_id""",
                    tuple(params),
                ).fetchall()
            result: list[dict] = []
            for row in rows:
                item = dict(row)
                # Служебная колонка только для сортировки: форма результата
                # должна остаться прежней (снимок резюме, без полей членства).
                item.pop("scope_rank", None)
                for field in (
                    "specializations",
                    "employment_types",
                    "work_formats",
                    "languages",
                    "education",
                ):
                    item[field] = json.loads(item[field])
                item["skills"] = [
                    {"name": skill["skill"], "proficiency": skill["proficiency"]}
                    for skill in conn.execute(
                        """SELECT skill, proficiency FROM competitor_resume_skills
                           WHERE resume_id = ? ORDER BY skill COLLATE NOCASE""",
                        (item["resume_id"],),
                    )
                ]
                result.append(item)
            return result

    def count_limited_competitor_runs(
        self,
        search_query: str | None = None,
        *,
        search_in: str | None = None,
        auth_mode: str | None = None,
    ) -> int:
        """Count queries whose latest finished collection has limited coverage.

        Scoped like ``list_competitor_resumes`` (#669): coverage of a
        ``position`` collection says nothing about a ``full_text`` one, so the
        warning must come from the run that produced the reported population.
        Legacy runs carry NULL and match the pre-scope defaults.
        """
        with self._connect() as conn:
            if search_query is None:
                row = conn.execute(
                    """SELECT COUNT(*) AS total
                       FROM competitor_collection_runs r
                       WHERE r.finished_at IS NOT NULL
                         AND r.rowid = (
                           SELECT latest.rowid
                           FROM competitor_collection_runs latest
                           WHERE latest.search_query = r.search_query
                             AND latest.finished_at IS NOT NULL
                           ORDER BY latest.started_at DESC, latest.rowid DESC
                           LIMIT 1
                         )
                         AND (r.status = 'limited'
                              OR r.detail LIKE '%limited_by_max_pages=1%')"""
                ).fetchone()
            else:
                conditions = ["search_query = ?", "finished_at IS NOT NULL"]
                params: list[str] = [search_query]
                # Обе половины отчёта обязаны одинаково понимать «легаси»: эти
                # COALESCE подставляют ровно то, чем миграция помечает строки
                # членства. Для search_in это факт `full_text` (pos был жёстко
                # зашит), для auth_mode — LEGACY_UNKNOWN_SCOPE, потому что режим
                # был выбираемым и в членстве не записывался. Разойдись они —
                # и `--auth-mode anonymous` предупреждал бы об ограниченном
                # покрытии выборки, в которой нет ни одной строки.
                if search_in is not None:
                    conditions.append("COALESCE(search_in, 'full_text') = ?")
                    params.append(search_in)
                if auth_mode is not None:
                    conditions.append("COALESCE(auth_mode, ?) = ?")
                    params.append(LEGACY_UNKNOWN_SCOPE)
                    params.append(auth_mode)
                row = conn.execute(
                    f"""SELECT CASE
                         WHEN status = 'limited' OR detail LIKE '%limited_by_max_pages=1%'
                         THEN 1 ELSE 0 END AS total
                       FROM competitor_collection_runs
                       WHERE {" AND ".join(conditions)}
                       ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                    tuple(params),
                ).fetchone()
            return int(row["total"] if row else 0)

    # --- Рынок вакансий: собранные карточки (#66, Этап 1) ----------------------
    # Новые методы в конец файла (паттерн with self._connect(), существующие
    # не трогаем). vacancies_seen — побочный эффект search: запись собранных
    # карточек, чтобы рынок-анализ (сравнение сфер по медианной ЗП) строился из
    # реальных данных, а не из эфемерного вывода консоли. Цель Этапа 1 (#65):
    # МАКСИМИЗАЦИЯ ДОХОДА — подсветить сферы с ВЫШЕ медианной зарплатой.

"""History — совместимый фасад над доменными миксинами хранилища (#1035).

Публичный API прежний: ``History``, ``SCHEMA``, ``SKIP_REASONS``,
``SKIP_REASON_VALUES``, ``REPLY_STATUS_VALUES``, ``CommandRunBusy`` и
приватные lease-хелперы, на которые опираются monkeypatch-тесты. Домены
живут в соседних модулях: схема/миграции — ``history_schema``, lease —
``history_lease``, ledger — ``history_ledger``, review — ``history_review``,
supervised-запуски — ``history_commands``, ответы работодателей —
``history_replies``, аналитика — ``history_analytics``, вакансии —
``history_vacancies``, конкуренты — ``history_competitors``, анкеты —
``history_questionnaires``, профиль — ``history_profile``. Схема БД, ORM и
транзакционные границы не менялись: каждый метод по-прежнему открывает
собственное соединение через ``_connect``.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .history_analytics import AnalyticsMixin
from .history_commands import CommandsMixin
from .history_competitors import CompetitorsMixin
from .history_lease import (  # noqa: F401  (ре-экспорт: monkeypatch-контракт тестов)
    LEGACY_LEASE_GRACE,
    CommandRunBusy,
    _parse_recorded_at,
    _pid_is_alive,
    _row_is_live,
)
from .history_ledger import LedgerMixin
from .history_profile import ProfileMixin
from .history_questionnaires import QuestionnairesMixin
from .history_replies import (  # noqa: F401  (ре-экспорт публичного API)
    REPLY_STATUS_VALUES,
    RESPONSES_ALERT_CHECKPOINT,
    RepliesMixin,
)
from .history_review import ReviewQueueMixin
from .history_schema import (  # noqa: F401  (_ensure_apply_index импортируют тесты)
    LEGACY_UNKNOWN_SCOPE,
    SCHEMA,
    _ensure_apply_index,
    _ensure_column,
    _migrate_competitor_query_scope_schema,
    _migrate_competitor_skills_schema,
    _purge_legacy_dry_run_applied_skips,
    _rename_apply_runs_to_command_runs,
)
from .history_skip_reasons import (  # noqa: F401  (ре-экспорт публичного API)
    SKIP_REASON_VALUES,
    SKIP_REASONS,
    _SkipReasons,
)
from .history_vacancies import VacanciesMixin

logger = logging.getLogger("hhru_bot.history")


class History(
    CommandsMixin,
    ReviewQueueMixin,
    LedgerMixin,
    RepliesMixin,
    AnalyticsMixin,
    VacanciesMixin,
    CompetitorsMixin,
    QuestionnairesMixin,
    ProfileMixin,
):
    # Feedback is deliberately bounded: it is prompt context, not an archive
    # of potentially sensitive letters.
    FEEDBACK_REASON_MAX = 500
    # SequenceMatcher can be quadratic for adversarial/repetitive input. Keep
    # the CLI bounded before doing any matching; the stored context is smaller
    # still (FEEDBACK_SNIPPET_MAX below).
    FEEDBACK_LETTER_MAX = 4000
    FEEDBACK_SNIPPET_MAX = 2000

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
        """Создаёт все таблицы (CREATE IF NOT EXISTS). Идемпотентно.

        CAVEAT (#51): CREATE TABLE IF NOT EXISTS НЕ добавляет колонку в уже
        существующую таблицу. Новые колонки в существующих таблицах добавляем
        через ALTER TABLE ADD COLUMN под идемпотентной обёрткой PRAGMA
        table_info (добавляем только если колонки ещё нет — иначе повторный
        запуск упадёт на 'duplicate column'). Это безопаснее пересоздания БД:
        не теряем историю откликов.
        """
        with self._connect() as conn:
            # #461: миграция старого имени таблицы ДО executescript(SCHEMA) —
            # CREATE TABLE IF NOT EXISTS command_runs внутри SCHEMA не должен
            # успеть создать пустую command_runs раньше RENAME, иначе RENAME
            # упадёт на "table command_runs already exists".
            _rename_apply_runs_to_command_runs(conn)
            # #669: по той же причине, что и RENAME выше — SCHEMA создаёт
            # idx_competitor_queries_query уже по новым колонкам, поэтому
            # пересборка ключа членства обязана пройти ДО executescript.
            #
            # Обе миграции ниже открывают собственный BEGIN IMMEDIATE, и это
            # безопасно ровно здесь: legacy-режим sqlite3 открывает неявную
            # транзакцию только на DML, а до них в этой функции идёт лишь DDL
            # (_rename_apply_runs_to_command_runs) и executescript, который сам
            # коммитит перед выполнением. Появится DML выше — вложенный BEGIN
            # упадёт на "cannot start a transaction within a transaction".
            _migrate_competitor_query_scope_schema(conn)
            conn.executescript(SCHEMA)
            _migrate_competitor_skills_schema(conn)
            _ensure_column(conn, "actions", "letter_variant", "TEXT")
            _ensure_column(conn, "actions", "search_query", "TEXT")
            _ensure_column(conn, "actions", "run_id", "TEXT")
            _ensure_column(conn, "actions", "reason_code", "TEXT")
            _ensure_column(conn, "responses", "last_invitation_at", "TEXT")
            _ensure_column(conn, "command_runs", "owner_pid", "INTEGER")
            # #654: competitor collection predates durable ownership/checkpoints.
            # Existing rows stay NULL and are handled with the same legacy grace
            # window as command_runs before they can be reclaimed.
            _ensure_column(conn, "competitor_collection_runs", "owner_pid", "INTEGER")
            # NULL marks legacy runs whose authentication scope is unknown.
            # They must never be selected as resume checkpoints for a new,
            # explicitly scoped collection.
            _ensure_column(conn, "competitor_collection_runs", "auth_mode", "TEXT")
            _ensure_column(conn, "competitor_collection_runs", "search_in", "TEXT")
            _ensure_column(conn, "competitor_collection_runs", "heartbeat_at", "TEXT")
            _ensure_column(conn, "competitor_collection_runs", "last_started_page", "INTEGER")
            _ensure_column(conn, "competitor_collection_runs", "last_completed_page", "INTEGER")
            _ensure_column(conn, "competitor_collection_runs", "resume_page", "INTEGER")
            _ensure_column(conn, "competitor_collection_runs", "resumed_from_run_id", "TEXT")
            _ensure_column(conn, "competitor_collection_runs", "observed_page_size", "INTEGER")
            _ensure_column(
                conn,
                "competitor_collection_runs",
                "requested_page_size",
                "INTEGER NOT NULL DEFAULT 100",
            )
            _ensure_column(conn, "competitor_collection_runs", "exit_code", "INTEGER")
            # #660 (Codex review): cards_seen already includes the in-progress
            # page's cards as soon as it's parsed, before that page's details
            # are all fetched -- but resume_page still points at that same
            # unfinished page. resume_rank_offset must exclude that page's
            # cards (it will be re-parsed from scratch on resume), so it is
            # computed from cards_seen_completed (cumulative cards as of the
            # last *completed* page), not from cards_seen. Legacy rows stay
            # NULL; begin_competitor_collection() falls back to cards_seen for
            # those (old behavior, unaffected by this fix).
            _ensure_column(conn, "competitor_collection_runs", "cards_seen_completed", "INTEGER")
            # #679: geography was absent from the original competitor snapshot.
            # NULL keeps existing rows valid until their next collection.
            _ensure_column(conn, "competitor_resumes", "area", "TEXT")
            _ensure_column(conn, "competitor_resumes", "relocation", "TEXT")
            _ensure_column(conn, "competitor_resumes", "business_trips", "TEXT")
            _ensure_column(conn, "competitor_resumes", "metro_station", "TEXT")
            # #473: questionnaire research snapshots predate the apply audit
            # fields.  CREATE TABLE IF NOT EXISTS leaves those old tables
            # untouched, so keep the migration explicitly idempotent.
            _ensure_column(conn, "questionnaire_scans", "source", "TEXT NOT NULL DEFAULT 'probe'")
            _ensure_column(conn, "questionnaire_questions", "answer", "TEXT")
            _ensure_column(conn, "questionnaire_questions", "answer_source", "TEXT")
            _ensure_column(conn, "questionnaire_questions", "confidence", "REAL")
            _ensure_column(conn, "questionnaire_questions", "filled", "INTEGER NOT NULL DEFAULT 0")
            # #482: аудит анкеты расширен полями резолвера. answer_source и
            # confidence добавлены ещё в #473 и здесь не дублируются.
            _ensure_column(conn, "questionnaire_questions", "template", "TEXT")
            _ensure_column(conn, "questionnaire_questions", "cluster", "TEXT")
            _ensure_column(conn, "questionnaire_questions", "resolver_source", "TEXT")
            _ensure_column(conn, "questionnaire_questions", "run_id", "TEXT")
            # #420 follow-up (Codex adversarial-review, PR #449): review_queue
            # rows created before this column existed have no stored search_query
            # — they stay NULL and are legacy-attributed via the existing
            # vacancies_seen fallback in funnel_by_search_query, same as actions.
            _ensure_column(conn, "review_queue", "search_query", "TEXT")
            # #93: employer_tier в vacancies_seen (для estimate_salary). CREATE TABLE
            # IF NOT EXISTS не добавит колонку в уже существующую таблицу (#51) —
            # поэтому ALTER'ом идемпотентно доводим старые базы.
            _ensure_column(conn, "vacancies_seen", "employer_tier", "TEXT")
            _ensure_column(conn, "vacancies_seen", "vacancy_text", "TEXT")
            _ensure_column(conn, "vacancies_seen", "published_at", "TEXT")
            # #517: доп. признаки карточки для статистики/ML на старых БД.
            _ensure_column(conn, "vacancies_seen", "address", "TEXT")
            _ensure_column(conn, "vacancies_seen", "is_remote", "INTEGER")
            _ensure_column(conn, "vacancies_seen", "experience", "TEXT")
            _ensure_column(conn, "vacancies_seen", "snippet_requirement", "TEXT")
            _ensure_column(conn, "vacancies_seen", "snippet_responsibility", "TEXT")
            # #516 priority-2: optional vacancy-card badges.
            _ensure_column(conn, "vacancies_seen", "side_job", "INTEGER")
            _ensure_column(conn, "vacancies_seen", "no_resume", "INTEGER")
            _ensure_column(conn, "vacancies_seen", "activity", "TEXT")
            _ensure_column(conn, "vacancies_seen", "hh_rating", "TEXT")
            _ensure_column(conn, "vacancies_seen", "hrbrand_winner", "INTEGER")
            _ensure_column(conn, "vacancies_seen", "metro_stations", "TEXT")
            # #177: CREATE UNIQUE INDEX IF NOT EXISTS не пересоздаст индекс с новым
            # WHERE-условием на уже существующей БД (тот же caveat #51, что и для
            # колонок) — старые базы содержат idx_resume_vacancy_apply без
            # 'uncertain' в условии. Доводим его по аналогии с _ensure_column:
            # сначала читаем текущее DDL из sqlite_master, DROP+CREATE только
            # если оно отличается от желаемого (иначе КАЖДЫЙ CLI-вызов делал бы
            # лишнюю write-миграцию с захватом schema-lock — cycle-review #177).
            _ensure_apply_index(conn)
            # #431: старые apply dry-run могли успеть закэшировать
            # ALREADY_APPLIED в skipped. Удаляем только такие записи, когда
            # для пары нет success/uncertain-действия; skip без dry-run или с
            # реальным действием сохраняется.
            _purge_legacy_dry_run_applied_skips(conn)

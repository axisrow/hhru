from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# Схема SQLite — одна константа, CREATE TABLE IF NOT EXISTS для всех таблиц.
# Системы миграций для такого маленького проекта не нужно (оверинжиниринг): при
# сильных изменениях схемы базу пересоздают заново (данных мало). _init_schema()
# применяет SCHEMA идемпотентно при каждом открытии — IF NOT EXISTS гарантирует,
# что повторный запуск на существующей базе не падает и не трогает данные.
SCHEMA = """\
-- actions — журнал откликов/поднятий резюме (append-only).
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_resume_vacancy_apply
    ON actions(resume_id, vacancy_id)
    WHERE action = 'apply' AND status IN ('success', 'dry_run');

-- responses — мониторинг ответов работодателей (#12, account-scope).
-- Одна строка НА ПЕРЕПИСКУ: текущий «свежий» статус ответа работодателя,
-- перезаписываемый при каждом fetch_responses (upsert_response). Ключ
-- UNIQUE(vacancy_id, topic): страница /applicant/negotiations общая по аккаунту,
-- карточка переписки НЕ несёт достоверного признака «какому резюме принадлежит
-- ответ» (resume_id опционален и НЕ входит в ключ). topic=NULL (ответ без чата)
-- группируется по vacancy_id — UNIQUE допускает несколько NULL.
CREATE TABLE IF NOT EXISTS responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT,
    vacancy_id TEXT NOT NULL,
    topic TEXT,
    employer TEXT,
    status TEXT NOT NULL,
    last_status TEXT,
    chat_url TEXT,
    response_date TEXT,
    last_seen_at TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (vacancy_id, topic)
);

CREATE INDEX IF NOT EXISTS idx_responses_status_changed_at
    ON responses(status_changed_at);

-- manual_offers — ручные пометки офферов (#13), ОТДЕЛЬНО от responses (#12).
-- responses перезаписывается каждым scrape'ом #12 и затёр бы ручной offer;
-- manual_offers — липкая ручная пометка, per-resume: UNIQUE(resume_id, vacancy_id).
CREATE TABLE IF NOT EXISTS manual_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    marked_at TEXT NOT NULL,
    UNIQUE (resume_id, vacancy_id)
);
"""


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
        """Создаёт все таблицы (CREATE IF NOT EXISTS). Идемпотентно.

        CAVEAT (#51): CREATE TABLE IF NOT EXISTS НЕ добавляет колонку в уже
        существующую таблицу. Новые колонки в существующих таблицах добавляем
        через ALTER TABLE ADD COLUMN под идемпотентной обёрткой PRAGMA
        table_info (добавляем только если колонки ещё нет — иначе повторный
        запуск упадёт на 'duplicate column'). Это безопаснее пересоздания БД:
        не теряем историю откликов.
        """
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            _ensure_column(conn, "actions", "letter_variant", "TEXT")

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
        letter_variant: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO actions
                    (resume_id, vacancy_id, action, status, reason, letter_variant, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resume_id,
                    vacancy_id,
                    action,
                    status,
                    reason,
                    letter_variant,
                    datetime.now().isoformat(),
                ),
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

    def list_actions(self, resume_id: str | None, period: str, limit: int = 50) -> list[dict]:
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

    # --- Мониторинг ответов работодателей (#12, Этап 2) ------------------------
    # Новые методы в конец файла (паттерн with self._connect(), существующие
    # не трогаем). responses — отдельная таблица (см. SCHEMA), хранит ПОСЛЕДНЕЕ
    # состояние переписки по (vacancy_id, topic), а не журнал переходов.
    # upsert перезаписывает статус только при смене; last_seen_at обновляется
    # всегда (каждый fetch_responses видел эту вакансию в списке).

    def upsert_response(
        self,
        vacancy_id: str,
        employer: str | None,
        status: str,
        chat_url: str | None,
        topic: str | None = None,
        response_date: str | None = None,
        resume_id: str | None = None,
    ) -> str:
        """Записывает/обновляет текущий статус ответа работодателя (account-scope).

        Ключ — ``(vacancy_id, topic)`` (одна строка на переписку). Страница
        /applicant/negotiations общая и НЕ несёт достоверного признака
        принадлежности ответа конкретному резюме, поэтому ответ НЕ клонируется
        под все resume_id (это фабриковало бы данные). Одна вакансия может дать
        НЕСКОЛЬКО переписок (разные topic, напр. отклик с разных резюме) — ключ
        по вакансии затирал бы соседние; topic (= id чата из chat_url) их
        различает. topic=None (ответ без чата) группируется по vacancy_id
        (SQLite UNIQUE допускает несколько NULL). ``resume_id`` опционален — под
        будущую достоверную атрибуцию, в ключ UNIQUE не входит.

        Возвращает одно из: ``"inserted"`` (строка заведена впервые),
        ``"updated"`` (статус сменился — это «новый ответ»: прежний status
        копируется в last_status, метка status_changed_at сдвигается),
        ``"unchanged"`` (строка была, статус тот же — обновляем только last_seen_at
        и response_date, как «свежий взгляд без изменений»).
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM responses WHERE vacancy_id = ? AND topic IS ?",
                (vacancy_id, topic),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO responses
                        (resume_id, vacancy_id, topic, employer, status, chat_url,
                         response_date, last_seen_at, status_changed_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resume_id,
                        vacancy_id,
                        topic,
                        employer,
                        status,
                        chat_url,
                        response_date,
                        now,
                        now,
                        now,
                    ),
                )
                return "inserted"
            if row["status"] != status:
                # Статус сменился: прежний → last_status, новый → status, двигаем
                # status_changed_at. employer/chat_url/response_date освежаются тоже
                # (работодатель мог смениться или hh.ru отдал свежую дату ответа).
                conn.execute(
                    """
                    UPDATE responses
                       SET resume_id = ?, employer = ?, last_status = status, status = ?,
                           chat_url = ?, response_date = ?, last_seen_at = ?,
                           status_changed_at = ?
                     WHERE vacancy_id = ? AND topic IS ?
                    """,
                    (
                        resume_id,
                        employer,
                        status,
                        chat_url,
                        response_date,
                        now,
                        now,
                        vacancy_id,
                        topic,
                    ),
                )
                return "updated"
            # Статус не изменился — освежаем только «когда последний раз видели»
            # и дату ответа (hh.ru мог обновить блок даты без смены статуса).
            conn.execute(
                "UPDATE responses SET resume_id = ?, employer = ?, chat_url = ?, "
                "response_date = ?, last_seen_at = ? WHERE vacancy_id = ? AND topic IS ?",
                (resume_id, employer, chat_url, response_date, now, vacancy_id, topic),
            )
            return "unchanged"

    def new_responses_since(self, since: datetime, resume_id: str | None = None) -> list[dict]:
        """Ответы работодателей, чей статус сменился после ``since``.

        «Новый ответ» = status_changed_at > since (включает впервые заведённые
        строки: у них status_changed_at == created_at). resume_id=None — по всем
        резюме. Свежие первыми. Возвращает словари с ключами resume_id/vacancy_id/
        topic/employer/status/last_status/chat_url/response_date/status_changed_at
        — для вывода команды responses.
        """
        where = ["status_changed_at > ?"]
        params: list = [since.isoformat()]
        if resume_id is not None:
            where.append("resume_id = ?")
            params.append(resume_id)
        clause = " WHERE " + " AND ".join(where)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT resume_id, vacancy_id, topic, employer, status, last_status, chat_url, "
                f"response_date, status_changed_at "
                f"FROM responses{clause} ORDER BY status_changed_at DESC, id DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Воронка и ручная пометка оффера (#13) ----------------------------
    # Воронка JOIN'ит actions × responses. Таблица responses — account-scope
    # (#12): ключ UNIQUE(vacancy_id, topic), resume_id опционален и НЕ в ключе
    # (страница /applicant/negotiations не несёт достоверного признака
    # принадлежности ответа конкретному резюме). Поэтому JOIN идёт по
    # vacancy_id, а группировка воронки — по actions.resume_id (где отклик
    # отправлен). status='offer' — ручная пометка командой mark (hh.ru оффер
    # как статус переговоров не отдаёт); остальных статусов (read/invitation/
    # discard/response) наполняет #12 через upsert_response из живых переговоров.

    @staticmethod
    def _pct(numerator: int, denominator: int) -> float:
        """Конверсия в процентах с защитой от деления на ноль: 0/0 → 0.0.

        Округление до 1 знака — для читаемого CLI-вывода (воронка — для людей).
        """
        if denominator <= 0:
            return 0.0
        return round(numerator / denominator * 100, 1)

    def mark_offer(self, vacancy_id: str, resume_id: str) -> bool:
        """Ручная пометка оффера — липкая, per-resume, в отдельной таблице.

        hh.ru не отдаёт оффер как статус переговоров, поэтому верхний шаг
        воронки заполняется вручную командой ``mark --vacancy <id> --status offer``.
        Хранится в ``manual_offers`` (НЕ в responses #12): responses перезаписывается
        каждым scrape'ом #12 и затёр бы ручной offer; manual_offers — липкая пометка,
        survives последующие scrape'ы. Ключ UNIQUE(resume_id, vacancy_id) — per-resume
        (resume_id обязателен). Возвращает True, если пометка создана, False — если
        уже была.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO manual_offers (resume_id, vacancy_id, marked_at) "
                "VALUES (?, ?, ?)",
                (resume_id, vacancy_id, now),
            )
            return cur.rowcount > 0

    def funnel_by_resume(
        self,
        since: str | None = None,
        resume_id: str | None = None,
    ) -> list[dict]:
        """Воронка отправлено → просмотрено → приглашение → оффер по резюме.

        Этапы КУМУЛЯТИВНЫЕ (sent ⊇ viewed ⊇ invited ⊇ offer): вакансия, до которой
        дошло приглашение, считается и просмотренной; оффер — и просмотренным, и
        приглашённым. Это необходимо, т.к. #12 хранит в responses только ТЕКУЩИЙ
        статус переписки (после read→invitation прежний read уже не виден) —
        некумулятивный подсчёт давал бы viewed=0 после перехода. «Просмотрено» =
        любой ответ работодателя (#12: read/response/invitation/discard/offer) —
        отказ или письмо тоже означают, что резюме видели.

        Ответы берутся из responses (#12, account-scope по vacancy_id) плюс липкие
        ручные пометки из manual_offers (per-resume). Группировка по actions.resume_id.
        Пер-резюме точность ограничена account-scope responses (ответ одной вакансии
        зачтётся всем резюме, откликнувшимся в неё) — это ограничение источника
        данных #12 (нет достоверного связывания ответ→резюме).

        Конверсии: view_rate=viewed/sent, invite_rate=invited/viewed, offer_rate=
        offer/invited; 0% при пустом знаменателе. Возвращает список словарей (по
        строке на resume_id, отсортированных по убыванию отправленных). Пусто → [].
        """
        where = ["a.action = 'apply'", "a.status = 'success'"]
        params: list = []
        if since is not None:
            where.append("a.created_at >= ?")
            params.append(since)
        if resume_id is not None:
            where.append("a.resume_id = ?")
            params.append(resume_id)
        clause = " WHERE " + " AND ".join(where)

        # EXISTS-подзапросы вместо тройного LEFT JOIN: нет декартова произведения
        # при нескольких responses-строках одной вакансии (разные topic), и этапы
        # кумулятивны по построению (каждый следующий INCLUDE-список шире).
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    a.resume_id AS resume_id,
                    COUNT(DISTINCT a.vacancy_id) AS sent,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('read', 'response', 'invitation', 'discard', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.vacancy_id END) AS viewed,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('invitation', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.vacancy_id END) AS invited,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id AND r.status = 'offer'
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.vacancy_id END) AS offer
                FROM actions AS a
                {clause}
                GROUP BY a.resume_id
                ORDER BY sent DESC, a.resume_id
                """,
                params,
            ).fetchall()

        funnel: list[dict] = []
        for row in rows:
            sent, viewed, invited, offer = row["sent"], row["viewed"], row["invited"], row["offer"]
            funnel.append(
                {
                    "resume_id": row["resume_id"],
                    "sent": sent,
                    "viewed": viewed,
                    "invited": invited,
                    "offer": offer,
                    "view_rate": self._pct(viewed, sent),
                    "invite_rate": self._pct(invited, viewed),
                    "offer_rate": self._pct(offer, invited),
                }
            )
        return funnel

    def dead_responses(self, days: int, resume_id: str | None = None) -> dict:
        """«Мёртвая зона»: доля откликов без ответа старше N дней.

        Кандидат на смену письма/резюме — отклик отправлен, но ответа от
        работодателя нет уже дольше ``days`` дней. «Отвеченный» = есть любая
        responses-строка по вакансии (включая ``read`` — работодатель посмотрел
        резюме, это валидный сигнал; invitation/discard/response — тем более).
        JOIN по vacancy_id (как в воронке, account-scope).

        total_sent здесь = отклики СТАРШЕ N дней (кандидаты стать мёртвыми), НЕ
        все отправленные (как в воронке) — поле переиспользовано, подпись в
        format_dead проясняет semantics. Возвращает {total_sent, dead, dead_rate};
        dead_rate в процентах (0.0 при пустой истории).
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        where = ["a.action = 'apply'", "a.status = 'success'", "a.created_at < ?"]
        params: list = [cutoff]
        if resume_id is not None:
            where.append("a.resume_id = ?")
            params.append(resume_id)
        clause = " WHERE " + " AND ".join(where)

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(DISTINCT a.vacancy_id) AS total_sent,
                    COUNT(DISTINCT CASE WHEN r.vacancy_id IS NULL
                                        THEN a.vacancy_id END) AS dead
                FROM actions AS a
                LEFT JOIN responses AS r ON r.vacancy_id = a.vacancy_id
                {clause}
                """,
                params,
            ).fetchone()

        total_sent = row["total_sent"] if row else 0
        dead = row["dead"] if row else 0
        return {
            "total_sent": total_sent,
            "dead": dead,
            "dead_rate": self._pct(dead, total_sent),
        }


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    """Идемпотентно добавляет колонку в существующую таблицу через ALTER TABLE.

    CREATE TABLE IF NOT EXISTS не добавляет колонку в уже созданную таблицу
    (#51 caveat). Эта функция проверяет наличие колонки через PRAGMA table_info
    и добавляет ALTER TABLE ADD COLUMN только если её нет — иначе повторный
    запуск History упал бы на 'duplicate column name'. Используется в
    _init_schema ПОСЛЕ executescript(SCHEMA).

    table/column/ddl_type интерполируются в DDL напрямую — это безопасно:
    значения caller-controlled (строковые литералы в коде истории), не ввод
    пользователя. Если хелпер когда-нибудь примет данные из конфига —
    потребуется валидация идентификатора.
    """
    # Нет таблицы → нечего дополнять (executescript(SCHEMA) должен был её
    # создать; если нет — это баг выше по потоку, не здесь).
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

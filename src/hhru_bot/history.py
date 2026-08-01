from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # SalaryInfo (доменный тип #34) возвращается estimate_salary (#93). Ленивый
    # импорт внутри метода разрывает цикл history <-> search (search тянет history
    # на верхнем уровне через SKIP_REASONS), здесь — только для type-checking.
    from .search import SalaryInfo

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

-- vacancies_seen — собранные карточки вакансий (#66, Этап 1: рынок).
-- search СОБИРАЕТ VacancyCard с зарплатой/датой (#34), но НЕ писал их в БД —
-- рынок-анализ (сравнение сфер по медианной ЗП) был не из чего строить. Эта
-- таблица — побочный эффект сбора: одна строка на (vacancy_id, search_query),
-- upsert по свежему scrape обновляет поля и двигает last_seen_at, first_seen_at
-- остаётся первым появлением. Зарплата из SalaryInfo (#34): salary_from/salary_to
-- оба NULL = «з/п не указана» (для доли рынка без зарплаты). Поля НЕ нормализуют
-- валюту в одну — разные сферы могут быть в USD/EUR/RUB, медиана считается в
-- рамках одного search_query (он обычно одной валюты).
-- employer_tier (#93) — уровень известности работодателя (KnownCompanyTier из
-- scoring.classify_employer: top_tech/big_corp/mid/unknown). Записывается при
-- сборе в commands/search._record_seen. Нужен для estimate_salary — эвристической
-- оценки ЗП вакансий без указанной: медиана salary_to по (search_query, tier).
-- Коэффициенты tier'ов считаются ИЗ ДАННЫХ (медианы по tier внутри сферы), а не
-- априорными константами — проверяет гипотезу «известные платят меньше».

CREATE TABLE IF NOT EXISTS vacancies_seen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vacancy_id TEXT NOT NULL,
    title TEXT,
    company TEXT,
    salary_from INTEGER,
    salary_to INTEGER,
    salary_currency TEXT,
    raw_date TEXT,
    search_query TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    employer_tier TEXT,
    UNIQUE (vacancy_id, search_query)
);

-- skipped — журнал отсева вакансий (#87, append-only).
-- filter_candidates логирует ``[skip] причина``, но НЕ писал её в БД → повторный
-- search пересматривал те же вакансии заново (трата LLM/времени, когда работают
-- pre-LLM фильтр #85 или LLM-скоринг #74). Эта таблица — кэш отсева: одна строка
-- на (resume_id, vacancy_id, reason). Partial-UNIQUE по этой тройке (как
-- actions/responses): один reason на пару, РАЗНЫЕ reasons — разные строки (вакансия
-- могла быть отсеяна по стоп-слову в одном запуске и как «уже откликались» в другом).
-- reason — стабильный enum-ключ (см. SKIP_REASONS), НЕ человекочитаемая строка
-- filter_candidates: маппинг строка→ключ делает feature-ишью (cli-spec §clear-skipped).
CREATE TABLE IF NOT EXISTS skipped (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (resume_id, vacancy_id, reason)
);

-- replies — журнал НАШИХ ответов работодателям в переписках (#108, решение #55).
-- ОТДЕЛЬНО от responses (#12) по той же причине, что manual_offers (#13):
-- responses перезаписывается каждым scrape'ом fetch_responses и затёр бы факт
-- нашей отправки. replies — append-only: одна строка на «ответ на конкретное
-- входящее».
-- inbound_marker — признак входящего сообщения, на которое отвечаем. Непрозрачная
-- для БД строка: реальный message_id, если hh.ru его отдаёт, иначе суррогат
-- (дата + хеш текста последнего входящего). Конкретный вид определяет вызывающий
-- по итогам probe --negotiations (#107) — схема НЕ завязана на один вариант.
-- ВАЖНО: replies — источник для аналитики и планирования, но НЕ единственный
-- источник правды об отправке. Перед боевой отправкой pipeline обязан свериться
-- с ЖИВЫМ чатом: пользователь мог ответить вручную с телефона, и БД об этом не
-- знает. has_replied отсекает заведомо отвеченные, живой чат подтверждает финально.
-- status — те же значения, что в actions (success/failed/dry_run), НЕ новый
-- словарь состояний: машины состояний (pending/in_flight/sent) по решению #55 нет.
-- Account-scope как responses: resume_id опционален и НЕ в ключе
-- (/applicant/negotiations не даёт достоверной привязки чата к резюме).
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    inbound_marker TEXT NOT NULL,
    vacancy_id TEXT,
    resume_id TEXT,
    status TEXT NOT NULL,
    letter_variant TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

-- Ключ PARTIAL-UNIQUE только по успешным ответам (тот же приём, что
-- idx_resume_vacancy_apply у actions). Так одно входящее не может получить два
-- успешных ответа, а dry_run/failed ключ НЕ занимают. Table-level
-- UNIQUE(topic, inbound_marker) здесь был бы багом: штатный сценарий «сначала
-- --dry-run, потом боевая отправка» (и ретрай после failed) молча терял бы
-- success под INSERT OR IGNORE — has_replied навсегда остался бы False, а
-- журнал потерял бы сам факт отправки. Неуспешные попытки при этом копятся
-- строками — это и есть материал для аналитики.
-- CAVEAT (#50, без миграций): если БД была создана ранней версией этой ветки с
-- table-level UNIQUE(topic, inbound_marker), CREATE TABLE IF NOT EXISTS её НЕ
-- переделает и старое ограничение останется рядом с новым индексом. Лечение по
-- решению проекта — удалить data/history.db и дать пересоздаться (данных мало).
CREATE UNIQUE INDEX IF NOT EXISTS idx_replies_topic_marker_success
    ON replies(topic, inbound_marker)
    WHERE status = 'success';

CREATE INDEX IF NOT EXISTS idx_replies_created_at ON replies(created_at);
"""


class _SkipReasons:
    """Стабильные enum-ключи причин отсева (#87, cli-spec §clear-skipped).

    Хранятся в ``skipped.reason`` и идут в ``--reason`` команды clear-skipped.
    НЕ человекочитаемые строки filter_candidates (``"уже откликались ранее"`` и
    т.п.) — маппинг строка→ключ делает ``filter_candidates`` в search.py. Так
    вывод фильтра остаётся локализованным для людей, а ключи в БД стабильны
    между запусками (cli-spec: ключи — проектируемый enum, привязанный к
    ПРИЧИНАМ filter_candidates, не к их строкам напрямую).

    Зарезервированы и будущие причины (#85 pre-LLM ``low_employer_signal`` и
    #84 ``has_questions``) — EnumExtension точка: новые значения добавляются
    сюда, миграций не требуется (``reason`` — свободный TEXT, валидация только
    на уровне команды clear-skipped через choices).
    """

    STOPWORD_TITLE = "stopword_title"  # exclude_keywords совпал в названии
    STOPWORD_EMPLOYER = "stopword_employer"  # exclude_employers — стоп-компания
    ALREADY_APPLIED = "already_applied"  # history.has_applied — уже откликались
    LOW_EMPLOYER_SIGNAL = "low_employer_signal"  # #85 pre-LLM фильтр (зарезервирован)
    LOW_LLM_SCORE = "low_llm_score"  # будущий отсев по LLM-скорингу #74
    HAS_QUESTIONS = "has_questions"  # #84 идея №7 (зарезервирован)
    DUPLICATE = "duplicate"  # дубликат вакансии в одном сборе


#: Enum-объект причин отсева. Используется как ``SKIP_REASONS.STOPWORD_TITLE``
#: — читаемее строковых литералов в filter_candidates/команде. Значения полей =
#: стабильные ключи в ``skipped.reason``.
SKIP_REASONS = _SkipReasons()

#: Все стабильные причины отсева (для ``--reason`` choices в clear-skipped и
#: валидации). Кортеж, не set — порядок стабилен для ``--help``.
SKIP_REASON_VALUES = (
    _SkipReasons.STOPWORD_TITLE,
    _SkipReasons.STOPWORD_EMPLOYER,
    _SkipReasons.ALREADY_APPLIED,
    _SkipReasons.LOW_EMPLOYER_SIGNAL,
    _SkipReasons.LOW_LLM_SCORE,
    _SkipReasons.HAS_QUESTIONS,
    _SkipReasons.DUPLICATE,
)


#: Допустимые значения ``replies.status`` (#108). Тот же словарь, что у actions
#: — новых состояний не вводим (решение #55: без машины состояний). Кортеж, не
#: set: порядок стабилен для сообщений об ошибке.
#:
#: Валидируется в record_reply намеренно (в отличие от record_action): опечатка
#: или синоним (``"SUCCESS"``, ``"sent"``) прошли бы в БД молча, has_replied
#: навсегда вернул бы False, и бот отправил бы работодателю ВТОРОЕ сообщение.
#: В actions такая же ошибка лишь искажает статистику, здесь — видна человеку.
REPLY_STATUS_VALUES = ("success", "failed", "dry_run")


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
            # #93: employer_tier в vacancies_seen (для estimate_salary). CREATE TABLE
            # IF NOT EXISTS не добавит колонку в уже существующую таблицу (#51) —
            # поэтому ALTER'ом идемпотентно доводим старые базы.
            _ensure_column(conn, "vacancies_seen", "employer_tier", "TEXT")

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

    # --- Рынок вакансий: собранные карточки (#66, Этап 1) ----------------------
    # Новые методы в конец файла (паттерн with self._connect(), существующие
    # не трогаем). vacancies_seen — побочный эффект search: запись собранных
    # карточек, чтобы рынок-анализ (сравнение сфер по медианной ЗП) строился из
    # реальных данных, а не из эфемерного вывода консоли. Цель Этапа 1 (#65):
    # МАКСИМИЗАЦИЯ ДОХОДА — подсветить сферы с ВЫШЕ медианной зарплатой.

    def upsert_vacancy_seen(
        self,
        vacancy_id: str,
        search_query: str,
        title: str | None = None,
        company: str | None = None,
        salary_from: int | None = None,
        salary_to: int | None = None,
        salary_currency: str | None = None,
        raw_date: str | None = None,
        employer_tier: str | None = None,
    ) -> None:
        """Записывает/освежает карточку вакансии по (vacancy_id, search_query).

        Ключ UNIQUE(vacancy_id, search_query): одна вакансия по разным поисковым
        запросам — отдельные строки (рынок хочет видеть, по каким запросам что
        находится и за сколько). При повторном scrape та же пара обновляет
        title/company/salary/raw_date (hh.ru мог поменять вилку) и двигает
        ``last_seen_at``; ``first_seen_at`` хранит ПЕРВОЕ появление и не трогается.

        Зарплата приходит из ``SalaryInfo`` (#34): ``salary_from``/``salary_to``
        оба NULL = «з/п не указана» (``parse_salary`` вернул None) — такая
        вакансия тоже пишется, для подсчёта доли рынка без зарплаты. Валюта НЕ
        нормализуется в одну: медиана считается в рамках одного search_query
        (внутри сферы валюта обычно однородна).

        ``employer_tier`` (#93) — уровень известности работодателя
        (``classify_employer``: top_tech/big_corp/mid/unknown). Записывается при
        сборе для группировки медианы в ``estimate_salary``. При обновлении
        существующей строки tier тоже освежается (компания могла получить бейдж
        trusted / накопить отзывы между scrape'ами).
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            # INSERT ... ON CONFLICT DO UPDATE: атомарный upsert по
            # UNIQUE(vacancy_id, search_query). first_seen_at — из исходной
            # строки (excluded.first_seen_at = текущий now, но ON CONFLICT
            # перезаписывает только перечисленные поля, first_seen_at не трогаем).
            conn.execute(
                """
                INSERT INTO vacancies_seen
                    (vacancy_id, title, company, salary_from, salary_to,
                     salary_currency, raw_date, search_query, first_seen_at,
                     last_seen_at, employer_tier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vacancy_id, search_query) DO UPDATE SET
                    title = excluded.title,
                    company = excluded.company,
                    salary_from = excluded.salary_from,
                    salary_to = excluded.salary_to,
                    salary_currency = excluded.salary_currency,
                    raw_date = excluded.raw_date,
                    employer_tier = excluded.employer_tier,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    vacancy_id,
                    title,
                    company,
                    salary_from,
                    salary_to,
                    salary_currency,
                    raw_date,
                    search_query,
                    now,
                    now,
                    employer_tier,
                ),
            )

    def list_vacancies_seen(self) -> list[dict]:
        """Все собранные вакансии, свежие первыми (по last_seen_at).

        Для диагностики и прямого SELECT из query (#45). Возвращает словари со
        всеми колонками таблицы.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT vacancy_id, title, company, salary_from, salary_to, salary_currency, "
                "raw_date, search_query, first_seen_at, last_seen_at, employer_tier "
                "FROM vacancies_seen ORDER BY last_seen_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Pre-LLM фильтр работодателя (#85) -----------------------------------
    # Новый метод в конец файла (паттерн with self._connect(), существующие
    # не трогаем). employer_interacted — позитивный сигнал для эвристического
    # pre-фильтра: работодатель УЖЕ проявлял интерес (приглашал/смотрел резюме),
    # значит отклик по новой вакансии от него — высокая конверсия, не отсекаем.
    # Источник — responses (#12, account-scope) + manual_offers (#13), JOIN по
    # vacancy_id (точный матч) и/или employer (имя компании, account-scope).

    def employer_interacted(
        self,
        vacancy_id: str | None = None,
        employer: str | None = None,
        resume_id: str | None = None,
    ) -> bool:
        """Был ли ранее интерес работодателя (приглашение/просмотр) — сигнал pre-фильтра (#85).

        Account-scope (как responses #12): НЕ требует resume_id. Проверяет по
        ``vacancy_id`` (точный матч — работодатель отвечал по ЭТОЙ вакансии) И/ИЛИ
        по ``employer`` (имя компании — работодатель когда-то отвечал по ЛЮБОЙ из
        своих вакансий). resume_id опционален и сужает manual_offers до резюме
        (responses и так account-scope, resume_id в их ключ не входит — #12).

        «Взаимодействие» = есть responses-строка с активным статусом работодателя
        (read/response/invitation/discard/offer — любой ответ = резюме видели) ИЛИ
        липкая ручная пометка оффера в manual_offers. Чистые вакансии без ответа
        (нет строки в responses) → False. Возвращает True при первом совпадении.
        """
        if vacancy_id is None and employer is None:
            return False

        clauses = []
        params: list = []
        # responses: активный статус работодателя (любой ответ). read включаем —
        # работодатель ПОСМОТРЕЛ резюме, это валидный сигнал интереса.
        clauses.append("status IN ('read', 'response', 'invitation', 'discard', 'offer')")
        if vacancy_id is not None:
            clauses.append("vacancy_id = ?")
            params.append(vacancy_id)
        if employer is not None:
            clauses.append("employer = ?")
            params.append(employer)
        responses_where = " AND ".join(clauses)

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM responses WHERE {responses_where} LIMIT 1",
                params,
            ).fetchone()
            if row is not None:
                return True

            # manual_offers: липкая ручная пометка оффера. resume_id обязателен в
            # таблице, но здесь опционален — без него учитываем все пометки.
            offer_clauses = []
            offer_params: list = []
            if vacancy_id is not None:
                offer_clauses.append("vacancy_id = ?")
                offer_params.append(vacancy_id)
            if resume_id is not None:
                offer_clauses.append("resume_id = ?")
                offer_params.append(resume_id)
            offer_where = (" WHERE " + " AND ".join(offer_clauses)) if offer_clauses else ""
            row = conn.execute(
                f"SELECT 1 FROM manual_offers{offer_where} LIMIT 1",
                offer_params,
            ).fetchone()
            return row is not None

    # Минимум вакансий с указанной ЗП, чтобы считать медиану сферы устойчивой.
    # Ниже порога сфера уходит вниз таблицы и помечается low_sample: на прогоне
    # #67 сфера с n=2 встала НАВЕРХУ как «лидер рынка» — сортировка по одной
    # медиане без учёта размера выборки вводит в заблуждение.
    _LOW_SAMPLE_N = 5

    def market_salary_by_query(self, include_estimates: bool = False) -> list[dict]:
        """Медианы зарплаты по поисковому запросу — сравнение сфер по доходу.

        Главная цель #66: ранжировать сферы по медианной ЗП. #125: считаются ДВЕ
        независимые медианы, потому что вилка на hh.ru часто односторонняя:

        * ``median_from`` / ``with_from`` — медиана нижних границ («от N»);
        * ``median_to`` / ``with_to`` — медиана верхних границ («до N» / фикс.).

        Раньше считалась только вторая, поэтому вакансии «от 350 000» не попадали
        в расчёт ВООБЩЕ (до 28% выборки, смещение до 20% — #125). Границы НЕ
        сливаются в один ряд (``COALESCE``): «от 300» и «до 300» — разные
        величины, их медиана не имеет смысла. Середина вилки не достраивается:
        у односторонних вакансий второй границы не существует, и подставлять её
        значило бы выдумывать данные.

        Медиана отсутствует (ни одной границы такого типа в доминирующей валюте)
        → 0; отчёт рисует «—».

        ``count`` = все собранные вакансии сферы, ``with_salary`` = сколько с
        ЛЮБОЙ указанной границей (покрытие: вакансия «от N» — это данные, а не
        пропуск). ``low_sample`` = True, если реальных ЗП меньше
        ``_LOW_SAMPLE_N`` — такие сферы сортируются ниже надёжных.

        Сортировка: сначала надёжные сферы по убыванию ``median_to``, затем
        ненадёжные (тоже по убыванию) — выгодные направления наверху, но не
        ценой того, что лидером станет строка на двух вакансиях.

        ``include_estimates`` (#93): если True — вакансии без указанной ЗП
        получают эвристическую оценку ``estimate_salary(search_query, tier)``
        (медиана по (query, tier) из данных) и включаются в медиану сферы. Так
        сферы, где большинство без ЗП, получают осмысленную медиану, а не 0/None.
        ВАЖНО (#125): оценка строится на ``salary_to``, т.е. это оценка ВЕРХНЕЙ
        границы — она достраивает только ``median_to``. В ``median_from`` оценки
        не подмешиваются, иначе верхняя граница выдавалась бы за нижнюю — ровно
        то смешение шкал, против которого заведён #125. Сфера, в медиану которой
        вошли оценки, помечается ``estimated=True`` — ``market_summary`` рисует
        перед её медианой ``~``. ``with_salary``/``with_from``/``with_to``
        остаются числами РЕАЛЬНЫХ ЗП (coverage доверия), независимо от оценок.

        Возвращает список словарей. Пусто → [].
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    v.search_query AS search_query,
                    -- #122: медианы считаются ТОЛЬКО по доминирующей валюте сферы.
                    -- salary_currency не нормализована: «от 6000 USD» рядом с
                    -- рублёвыми вилками занижало бы медиану, причём незаметно.
                    -- #125: доминирующая валюта — ОДНА на сферу, считается по
                    -- вакансиям с ЛЮБОЙ границей. Считать её отдельно для каждой
                    -- медианы нельзя: в отчёте одна колонка «Валюта», и медианы
                    -- в разных валютах под общей пометкой врали бы читателю.
                    (
                        SELECT salary_currency FROM vacancies_seen
                        WHERE search_query = v.search_query
                          AND (salary_from IS NOT NULL OR salary_to IS NOT NULL)
                        GROUP BY salary_currency
                        ORDER BY COUNT(*) DESC, salary_currency
                        LIMIT 1
                    ) AS currency,
                    COUNT(*) AS count,
                    -- Покрытие: вакансия с ЛЮБОЙ границей — это данные. До #125
                    -- здесь был COUNT(salary_to), и «от 350 000» считалась
                    -- вакансией без ЗП.
                    SUM(
                        CASE WHEN v.salary_from IS NOT NULL OR v.salary_to IS NOT NULL
                        THEN 1 ELSE 0 END
                    ) AS with_salary,
                    -- Сколько вакансий сферы имеют ЗП в ДРУГОЙ валюте (не вошли
                    -- ни в одну медиану) — чтобы отчёт мог честно об этом сказать.
                    SUM(
                        CASE WHEN (v.salary_from IS NOT NULL OR v.salary_to IS NOT NULL)
                          AND v.salary_currency IS NOT (
                            SELECT salary_currency FROM vacancies_seen
                            WHERE search_query = v.search_query
                              AND (salary_from IS NOT NULL OR salary_to IS NOT NULL)
                            GROUP BY salary_currency
                            ORDER BY COUNT(*) DESC, salary_currency
                            LIMIT 1
                        ) THEN 1 ELSE 0 END
                    ) AS other_currency
                FROM vacancies_seen AS v
                GROUP BY v.search_query
                """
            ).fetchall()
            out: list[dict] = []
            for row in rows:
                query = row["search_query"]
                currency = row["currency"]
                # Обе медианы — одним и тем же хелпером по своей колонке, оба раза
                # с фильтром доминирующей валюты (#122 применяется к ОБЕИМ).
                median_from, with_from = self._median_bound(
                    conn,
                    "salary_from",
                    "search_query = ? AND salary_currency IS ?",
                    [query, currency],
                )
                median_to, with_to = self._median_bound(
                    conn,
                    "salary_to",
                    "search_query = ? AND salary_currency IS ?",
                    [query, currency],
                )
                entry = {
                    "search_query": query,
                    "median_from": median_from or 0,
                    "median_to": median_to or 0,
                    "with_from": with_from,
                    "with_to": with_to,
                    "count": row["count"],
                    "with_salary": row["with_salary"] or 0,
                    "currency": currency,
                    "other_currency": row["other_currency"] or 0,
                    "estimated": False,
                }
                if include_estimates:
                    self._augment_with_estimates(conn, entry)
                entry["low_sample"] = (entry["with_salary"] or 0) < self._LOW_SAMPLE_N
                out.append(entry)
            # Сортировка в Python (а не в SQL): при include_estimates медиана
            # меняется уже после SELECT, а low_sample — производное поле. Ключ:
            # надёжные сферы выше ненадёжных, внутри группы — по убыванию
            # median_to, тай-брейк по count и search_query (стабильный порядок).
            out.sort(
                key=lambda e: (
                    e["low_sample"],
                    -e["median_to"],
                    -e["count"],
                    e["search_query"],
                )
            )
            return out

    def _augment_with_estimates(self, conn, entry: dict) -> None:
        """#93: если в сфере есть вакансии БЕЗ верхней границы — достраивает
        ``median_to`` их оценками.

        Берёт все вакансии сферы без salary_to, для каждой считает
        ``estimate_salary``-медиану по её tier (через ``_median_salary_to`` на
        том же соединении — без рекурсивного open), и если оценки есть —
        пересчитывает медиану сферы по РЕАЛЬНЫМ ЗП + оценкам, помечая
        ``estimated=True``. Чистая медиана реальных ЗП (без вакансий без ЗП)
        остаётся ``with_salary``-покрытием; оценка НЕ подменяет реальную, а
        достраивает картину для сфер, где реальных ЗП мало/нет.

        #125: оценка построена на ``salary_to``, т.е. это оценка ВЕРХНЕЙ границы —
        она трогает ТОЛЬКО ``median_to``. ``median_from`` остаётся медианой
        реальных нижних границ: подмешать в неё оценку верхней значило бы
        смешать две разные шкалы, против чего и заведён #125. Следствие: у
        сферы, где есть только вакансии «от N», оценивать верх не из чего, и
        ``median_to`` честно остаётся пустым.
        """
        query = entry["search_query"]
        # Реальные salary_to сферы (уже есть) + оценки для вакансий без ЗП.
        # #122: только доминирующая валюта — та же, по которой посчитана медиана
        # в market_salary_by_query, иначе оценки вернули бы смешение валют назад.
        real = [
            r["salary_to"]
            for r in conn.execute(
                "SELECT salary_to FROM vacancies_seen "
                "WHERE search_query = ? AND salary_to IS NOT NULL "
                "AND salary_currency IS ?",
                [query, entry.get("currency")],
            ).fetchall()
        ]
        # Вакансии без ЗП — их tier'ы (для оценки).
        no_salary_tiers = [
            r["employer_tier"]
            for r in conn.execute(
                "SELECT employer_tier FROM vacancies_seen "
                "WHERE search_query = ? AND salary_to IS NULL",
                [query],
            ).fetchall()
        ]
        if not no_salary_tiers:
            return  # все вакансии с ЗП — оценки не нужны, медиана реальная.

        # Оценка одна на tier внутри сферы (медиана по (query, tier)).
        # ВНИМАНИЕ: здесь, в агрегате сферы, НЕ применяется порог _ESTIMATE_TIER_MIN_N
        # (в отличие от точечной estimate_salary): на уровне сферы бёрём любую
        # доступную tier-информацию (медиана по tier, иначе вся сфера), т.к. оценки
        # взвешиваются количеством и сходятся к реальной медиане. estimate_salary —
        # точечная оценка одной вакансии, там порог n>=5 отсекает шумный tier.
        # #122: оценки — тоже ТОЛЬКО в доминирующей валюте сферы. Фильтр по
        # валюте нужен на обоих входах (tier-медиана и fallback на всю сферу):
        # без него рублёвая вилка попадала в медиану, помеченную как USD, и
        # смешение валют возвращалось через путь оценок — ровно тот перекос,
        # ради которого заведён #122. Валюта та же, что в market_salary_by_query.
        currency = entry.get("currency")
        tier_estimate: dict[str, int] = {}
        for tier in set(t for t in no_salary_tiers if t):
            med, _ = self._median_salary_to(
                conn,
                "search_query = ? AND employer_tier = ? AND salary_currency IS ?",
                [query, tier, currency],
            )
            if med is None:
                # fallback на всю сферу (в той же валюте).
                med, _ = self._median_salary_to(
                    conn,
                    "search_query = ? AND salary_currency IS ?",
                    [query, currency],
                )
            if med is not None:
                tier_estimate[tier] = med

        if not tier_estimate and not real:
            return  # оценок и реальных ЗП нет — оставляем как есть (0).

        combined = list(real)
        used_estimate = False
        for tier in no_salary_tiers:
            est = tier_estimate.get(tier or "")  # tier может быть NULL
            if est is not None:
                combined.append(est)
                used_estimate = True
            elif tier_estimate:
                # tier NULL/незнакомый, но оценки по др. tier есть → средняя оценка сферы.
                combined.append(sum(tier_estimate.values()) // len(tier_estimate))
                used_estimate = True

        if not combined:
            return
        combined.sort()
        n = len(combined)
        # Медиана тем же приёмом, что SQL-путь (AVG двух центральных, потом int):
        # _median_salary_to делает int(AVG(...)), здесь — int((a+b)/2) с round,
        # чтобы обе ветки считали медиану одинаково (без расхождения на 0.5).
        if n % 2 == 1:
            median = combined[n // 2]
        else:
            median = round((combined[n // 2 - 1] + combined[n // 2]) / 2)
        entry["median_to"] = int(median)
        if used_estimate:
            entry["estimated"] = True

    # --- Эвристическая оценка ЗП для вакансий без указанной (#93, часть B) -----
    #
    # ~50% вакансий на hh.ru РЕАЛЬНО без ЗП. Для рынок-анализа по доходу (#66)
    # нужны оценки. Гипотеза пользователя: «известные компании платят меньше,
    # потому что известные» (бренд-наценка наоборот). Это ГИПОТЕЗА — поэтому
    # коэффициенты tier'ов считаются ИЗ ДАННЫХ (медиана salary_to по
    # (search_query, tier)), а НЕ априорными константами «top_tech × 1.5».
    # Если данные покажут «top_tech < unknown» — эвристика это отразит; если по
    # tier мало данных (n<5) — fallback на медиану по всей сфере.

    # Минимум вакансий с ЗП по (query, tier), чтобы доверять tier-оценке, а не
    # падать на сферу. Мало данных → медиана по tier шумная → честнее сфера.
    _ESTIMATE_TIER_MIN_N = 5

    # Колонки-границы, по которым разрешено считать медиану. Список закрытый:
    # имя колонки подставляется в SQL текстом (параметром колонку не задать), и
    # белый список — граница между «внутренний хелпер» и SQL-инъекцией.
    _BOUND_COLUMNS = ("salary_from", "salary_to")

    def _median_bound(
        self, conn, column: str, where_clause: str, params: list
    ) -> tuple[int | None, int]:
        """Медиана одной границы вилки (``salary_from``/``salary_to``) + её n.

        Возвращает (median, count) — count это число строк, где ЭТА граница
        указана, а не число вакансий: у медиан «от» и «до» выборки разные (#125).
        Нет ни одного значения → (None, 0).

        Медиана — percentile через AVG двух центральных строк (тот же приём, что
        был в market_salary_by_query, вынесенный сюда, чтобы обе границы считались
        одинаково и без дублирования SQL).

        ВАЖНО: count берём из ``COUNT(*) OVER ()`` окна (число строк с ЗП в группе),
        а НЕ внешним ``COUNT(*)`` — внешний работает уже после ``WHERE rn IN (...)``
        (1-2 центральные строки) и давал бы 1/2, а не реальное число значений.
        """
        if column not in self._BOUND_COLUMNS:
            raise ValueError(f"недопустимая колонка границы: {column!r}")
        row = conn.execute(
            f"""
            SELECT AVG({column}) AS median, MAX(total) AS cnt
            FROM (
                SELECT {column}, ROW_NUMBER() OVER (ORDER BY {column}) AS rn,
                       COUNT(*) OVER () AS total
                FROM vacancies_seen
                WHERE {column} IS NOT NULL AND {where_clause}
            )
            WHERE rn IN ((total + 1) / 2, (total + 2) / 2)
            """,
            params,
        ).fetchone()
        if row is None or not row["cnt"]:
            return None, 0
        median = row["median"]
        return (int(median) if median else None, row["cnt"])

    def _median_salary_to(self, conn, where_clause: str, params: list) -> tuple[int | None, int]:
        """Медиана salary_to по произвольному условию + число строк с ЗП.

        Тонкая обёртка над :meth:`_median_bound` — оставлена как точка входа
        эвристических оценок (#93), которые строятся именно на верхней границе.
        """
        return self._median_bound(conn, "salary_to", where_clause, params)

    def estimate_salary(self, search_query: str, employer_tier: str) -> SalaryInfo | None:
        """Эвристическая оценка ЗП для вакансии БЕЗ указанной (#93, часть B).

        Считает медиану ``salary_to`` по собранным вакансиям сферы ``search_query``
        и tier'а ``employer_tier`` (top_tech/big_corp/mid/unknown). Коэффициенты
        tier'ов — ИЗ ДАННЫХ (медиана по tier внутри сферы), не априорные константы:
        если на практике «top_tech платит меньше» — оценка для top_tech будет
        ниже, гипотеза проверяется данными, а не угадывается.

        Fallback по убыванию доверия:
          1. Медиана по (search_query, tier), если по tier достаточно данных
             (n >= ``_ESTIMATE_TIER_MIN_N``). Наиболее точная оценка под конкретный
             tier работодателя.
          2. Иначе (мало данных по tier) — медиана по всей сфере (search_query,
             любой tier). Грубее, но не нулевая.
          3. Иначе — None (данных вообще нет, оценки не существует).

        Возвращает ``SalaryInfo`` (#34) с from=to=медиана (фиксированная оценка),
        currency подобранная из сферы (любая непустая), либо None. ``SalaryInfo``
        импортируется лениво — разрыв цикла history ↔ search (search тянет history
        на верхнем уровне через SKIP_REASONS).

        Это derived-view: оценка честно отличается от реальной ЗП пометкой
        ``~оценка`` в выводе (см. report_market.market_summary).
        """
        from .search import SalaryInfo

        with self._connect() as conn:
            # 1. Медиана по (query, tier).
            median, n_tier = self._median_salary_to(
                conn, "search_query = ? AND employer_tier = ?", [search_query, employer_tier]
            )
            source_tier = False
            if median is not None and n_tier >= self._ESTIMATE_TIER_MIN_N:
                source_tier = True

            # 2. Fallback на всю сферу, если по tier мало/нет данных.
            if not source_tier:
                median, _ = self._median_salary_to(conn, "search_query = ?", [search_query])

            if median is None:
                return None

            # currency — любая непустая в сфере (внутри search_query она обычно
            # однородна, см. upsert_vacancy_seen). NULL неприемлем для SalaryInfo.
            currency_row = conn.execute(
                "SELECT salary_currency FROM vacancies_seen "
                "WHERE search_query = ? AND salary_currency IS NOT NULL LIMIT 1",
                [search_query],
            ).fetchone()
            currency = currency_row["salary_currency"] if currency_row else "RUB"

        return SalaryInfo(
            salary_from=median,
            salary_to=median,
            currency=currency,
            raw=f"~оценка {median} {currency}",
        )

    # --- Журнал отсева skipped (#87) ------------------------------------------
    # Отдельный слой в конец файла (паттерн with self._connect(), существующие
    # методы не трогаем). skipped — append-only кэш отсева filter_candidates:
    # повторный search видит «уже отсеяна» и не дёргает LLM/фильтры повторно
    # (экономия #74/#85). Ключ UNIQUE(resume_id, vacancy_id, reason): разные
    # причины — разные строки, как actions/responses. record_skip идемпотентен
    # по UNIQUE (INSERT OR IGNORE). Координируется с #85 (pre-LLM фильтр пишет
    # свои причины сюда же) — слой общий, точки записи не конфликтуют.

    def record_skip(self, resume_id: str, vacancy_id: str, reason: str) -> None:
        """Записывает причину отсева вакансии (идемпотентно по UNIQUE).

        ``reason`` — стабильный enum-ключ из :data:`SKIP_REASONS` (НЕ
        человекочитаемая строка filter_candidates — маппинг делает вызывающий).
        Повторная запись той же (resume_id, vacancy_id, reason) — no-op
        (INSERT OR IGNORE под partial-UNIQUE): кэш не раздувается дублями при
        повторных search. Разные причины на одну пару — разные строки.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO skipped (resume_id, vacancy_id, reason, created_at) "
                "VALUES (?, ?, ?, ?)",
                (resume_id, vacancy_id, reason, datetime.now().isoformat()),
            )

    def is_skipped(self, resume_id: str, vacancy_id: str) -> bool:
        """True, если вакансия отсеяна по ЛЮБОЙ причине для этого резюме."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM skipped WHERE resume_id = ? AND vacancy_id = ? LIMIT 1",
                (resume_id, vacancy_id),
            ).fetchone()
            return row is not None

    def is_skipped_for(self, resume_id: str, vacancy_id: str, reason: str) -> bool:
        """True, если вакансия отсеяна по КОНКРЕТНОЙ причине."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM skipped "
                "WHERE resume_id = ? AND vacancy_id = ? AND reason = ? LIMIT 1",
                (resume_id, vacancy_id, reason),
            ).fetchone()
            return row is not None

    def clear_skipped(self, reason: str | None = None) -> int:
        """Удаляет записи отсева, возвращает число удалённых строк.

        ``reason=None`` — чистит всё (любые причины). Иначе — только строки с
        этой причиной. Используется командой clear-skipped (cli-spec §clear-skipped);
        возвращает число для вывода ``[OK] Удалено N``.
        """
        with self._connect() as conn:
            if reason is None:
                cur = conn.execute("DELETE FROM skipped")
            else:
                cur = conn.execute("DELETE FROM skipped WHERE reason = ?", (reason,))
            return cur.rowcount

    def count_skipped(self, reason: str | None = None) -> int:
        """Число записей отсева (для dry-run/подтверждения clear-skipped).

        ``reason=None`` — все причины, иначе — только указанная. Не удаляет.
        """
        with self._connect() as conn:
            if reason is None:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM skipped").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM skipped WHERE reason = ?", (reason,)
                ).fetchone()
            return row["cnt"] if row else 0

    # --- Журнал ответов работодателям replies (#108, решение #55) -------------
    # Отдельный слой в конец файла (паттерн with self._connect(), существующие
    # методы не трогаем). replies — append-only журнал НАШИХ ответов в чатах
    # negotiations, отдельно от перезаписываемой responses (#12). Ключ —
    # partial-UNIQUE(topic, inbound_marker) WHERE status='success': одно входящее
    # не получит двух успешных ответов, повторный success — no-op (INSERT OR
    # IGNORE), а dry_run/failed ключ не занимают и копятся для аналитики.
    # Account-scope: resume_id опционален и не в ключе.
    #
    # ГРАНИЦА ОТВЕТСТВЕННОСТИ (#55): этот слой отвечает «мы уже писали ответ на
    # это входящее», а НЕ «в чате уже есть наш ответ». Второе знает только живой
    # чат (пользователь мог ответить вручную с телефона). Планирование отсекает
    # по has_replied дёшево, боевая отправка обязана свериться с чатом в точке
    # отправки. Не превращать этот слой в единственный источник правды.

    def record_reply(
        self,
        topic: str,
        inbound_marker: str,
        *,
        vacancy_id: str | None = None,
        resume_id: str | None = None,
        status: str,
        letter_variant: str | None = None,
        note: str | None = None,
    ) -> None:
        """Записывает наш ответ на входящее сообщение (идемпотентно по UNIQUE).

        ``inbound_marker`` — непрозрачный признак входящего: реальный message_id
        либо суррогат (дата + хеш текста), см. комментарий к таблице. ``status``
        — из словаря actions (``success``/``failed``/``dry_run``), новых состояний
        не вводим (#55).

        Идемпотентность — по partial-UNIQUE, то есть только по УСПЕШНЫМ ответам:
        повторный ``success`` на ту же (topic, inbound_marker) — no-op (INSERT OR
        IGNORE), первая успешная запись не перезаписывается. Неуспешные попытки
        (``dry_run``/``failed``) ключ не занимают: они копятся строками для
        аналитики и НЕ блокируют последующий ``success`` — иначе штатный сценарий
        «сначала --dry-run, потом боевая отправка» терял бы факт отправки. Разные
        входящие в одном чате — разные строки (диалог продолжается).

        :raises ValueError: ``status`` вне :data:`REPLY_STATUS_VALUES`.
        """
        if status not in REPLY_STATUS_VALUES:
            raise ValueError(
                f"недопустимый status={status!r} для replies; "
                f"ожидается одно из {REPLY_STATUS_VALUES}"
            )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO replies
                    (topic, inbound_marker, vacancy_id, resume_id, status,
                     letter_variant, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    topic,
                    inbound_marker,
                    vacancy_id,
                    resume_id,
                    status,
                    letter_variant,
                    note,
                    datetime.now().isoformat(),
                ),
            )

    def has_replied(self, topic: str, inbound_marker: str) -> bool:
        """True, если мы УСПЕШНО ответили на это входящее (для планирования).

        Только ``status='success'``: ``dry_run`` и ``failed`` отправкой не
        считаются. Это намеренно ИНАЧЕ, чем в :meth:`has_applied` (#3), где
        dry_run дедуплицирует отклик: там повторный отклик безвреден, а здесь
        холостой прогон навсегда заблокировал бы боевой ответ на живое входящее.

        НЕ финальная проверка перед отправкой — см. границу ответственности выше:
        False здесь не значит «в чате нет нашего ответа».
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM replies "
                "WHERE topic = ? AND inbound_marker = ? AND status = 'success' LIMIT 1",
                (topic, inbound_marker),
            ).fetchone()
            return row is not None

    def replies_since(self, since: datetime) -> list[dict]:
        """Наши ответы, записанные после ``since`` — для аналитики и отчётов.

        Свежие первыми. Возвращает ВСЕ статусы (включая ``dry_run``/``failed``):
        журнал полный, фильтр «успешных» — задача вызывающего. Ключи словарей:
        topic/inbound_marker/vacancy_id/resume_id/status/letter_variant/note/
        created_at.

        ``since`` — НАИВНЫЙ datetime в локальном времени (как ``datetime.now()``,
        которым пишется ``created_at``): сравнение идёт лексикографически по
        ISO-строке, и tz-aware значение (суффикс ``+00:00``) дало бы мусорный
        результат. Граница ИСКЛЮЧАЮЩАЯ (``>``, как в new_responses_since).
        Не курсор: ``isoformat()`` опускает микросекунды, когда они ровно нули,
        поэтому передача ``created_at`` последней строки как ``since`` может
        пропустить строку той же секунды — для дозапроса фильтруй по id.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT topic, inbound_marker, vacancy_id, resume_id, status,
                       letter_variant, note, created_at
                FROM replies
                WHERE created_at > ?
                ORDER BY created_at DESC, id DESC
                """,
                (since.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]


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

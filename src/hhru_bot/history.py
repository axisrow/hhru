from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .external_forms.detect import normalize

if TYPE_CHECKING:
    # SalaryInfo (доменный тип #34) возвращается estimate_salary (#93). Ленивый
    # импорт внутри метода разрывает цикл history <-> search (search тянет history
    # на верхнем уровне через SKIP_REASONS), здесь — только для type-checking.
    from .search import SalaryInfo

logger = logging.getLogger("hhru_bot.history")

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
    search_query TEXT,
    run_id TEXT,
    reason_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apply_runs (
    run_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    requested_limit INTEGER,
    status TEXT NOT NULL,
    attempted INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    uncertain INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_apply_runs_status ON apply_runs(status, started_at);

-- #177: 'uncertain' тоже дедуплицируется в has_applied() (клик мог реально
-- уйти на hh.ru, статус неизвестен) — индекс обязан покрывать этот статус,
-- иначе гонка/повтор вставит несколько uncertain-строк для одной пары.
-- dry_run намеренно отсутствует: предпросмотр ничего не отправляет и не
-- должен блокировать последующий боевой отклик.
CREATE UNIQUE INDEX IF NOT EXISTS idx_resume_vacancy_apply
    ON actions(resume_id, vacancy_id)
    WHERE action = 'apply' AND status IN ('success', 'uncertain');

-- feedback — ручная обратная связь по вакансии (#417). Это отдельная таблица,
-- а не payload approval queue: reject можно записать до появления очереди и
-- не смешивать пользовательский текст с общей историей действий.
CREATE TABLE IF NOT EXISTS vacancy_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    edited_snippet TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vacancy_feedback_created_at
    ON vacancy_feedback(created_at);

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

-- resume_views — реальные просмотры резюме работодателями (#415).
-- Один snapshot на (резюме, событие просмотра, момент просмотра): повторный
-- scrape не раздувает счётчики, но сохраняет наблюдения для дневного тренда.
-- Источник данных — только SSR (applicantResumeViewHistory.historyViews),
-- см. resume_views.py::parse_resume_view_history; DOM-fallback намеренно
-- убран (#428 review, round 11) — его identity-модель (employer_id/link)
-- была структурно несовместима с SSR-моделью (source_id), и оба порядка
-- приоритета в view_key либо теряли данные, либо плодили дубликаты при
-- переключении между источниками между прогонами. Один источник истины
-- устраняет противоречие, а не откладывает его очередным гейтом.
-- Идентичность события — view_key (NOT NULL): source_id, если он есть,
-- иначе employer_id, иначе '' — этот последний fallback (оба поля пусты)
-- сейчас недостижим через штатный путь parse_resume_view_history (#428
-- review, round 12: парсер требует source_id или employer_id и иначе
-- бросает ValueError раньше вставки), но record_resume_views — отдельная
-- публичная функция, и НЕ гарантирует, что каждый вызывающий прошёл через
-- парсер; '' — безопасный defensive-дефолт, а не документированный
-- нормальный путь. source_id — стабильный per-view SSR id
-- (id/viewId/eventId). source_id в приоритете над employer_id: SSR-дата
-- часто без времени суток, и два разных просмотра ОДНОГО работодателя в
-- один день иначе получили бы одинаковый (employer_id, viewed_at) и
-- второй был бы молча отброшен INSERT OR IGNORE (#428 review). Ключ НЕ
-- включает employer — это mutable presentation-строка (имя могло
-- смениться, разное форматирование), и раньше её участие в UNIQUE плодило
-- дубликаты одного и того же просмотра (#428 review). employer_id/
-- source_id — NOT NULL пустой строкой, а не NULL: SQLite считает
-- несколько NULL различными значениями, и вернувшись к NULL здесь дедуп
-- скрытых просмотров снова сломался бы (#428 review: "preserve hidden
-- resume view events").
CREATE TABLE IF NOT EXISTS resume_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    employer_id TEXT,
    employer TEXT,
    view_key TEXT NOT NULL,
    viewed_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE (resume_id, view_key, viewed_at)
);

CREATE INDEX IF NOT EXISTS idx_resume_views_viewed_at ON resume_views(viewed_at);

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

-- account_profile — единый профиль аккаунта для автозаполнения внешних форм
-- (#282). Одинаковый вопрос может иметь два значения: ручное значение имеет
-- приоритет над автоматически считанным с hh.ru, но строки хранятся отдельно.
CREATE TABLE IF NOT EXISTS account_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (question_key, source)
);

-- settings — произвольные пользовательские настройки CLI (#383).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    search_query TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    employer_tier TEXT,
    vacancy_text TEXT,
    published_at TEXT,
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

-- review_queue — immutable, per-vacancy approval snapshots (#414).
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    vacancy_url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    score REAL NOT NULL,
    breakdown TEXT NOT NULL,
    letter TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    permit_hash TEXT,
    permit_expires_at TEXT,
    search_query TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status, id);

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
-- status — success/failed/dry_run/uncertain (#201); uncertain означает клик без
-- пойманного позитивного сигнала за таймаут и не дедуплицирует чат.
-- resume_id опционален и НЕ в ключе. ВАЖНО (#200): это НЕ значит «привязки к
-- резюме не существует» — прежняя формулировка («/applicant/negotiations не даёт
-- достоверной привязки чата к резюме») опровергнута живой проверкой 2026-08-16:
-- SSR topicList[] отдаёт resumeId у 7/7 переписок, и record_reply_and_action его
-- теперь пишет. Опциональность осталась как защита от дрейфа разметки: если hh.ru
-- перестанет отдавать поле, журналирование ответа не должно падать (NULL здесь,
-- пустой сентинел в actions.resume_id, который NOT NULL). В ключ не входит,
-- потому что ключ — (topic, inbound_marker): один ответ на одно входящее.
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
-- успешных ответа, а dry_run/failed/uncertain ключ НЕ занимают. Table-level
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

CREATE TABLE IF NOT EXISTS robot_questionnaires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL UNIQUE,
    vacancy_id TEXT,
    reason TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_robot_questionnaires_detected_at
    ON robot_questionnaires(detected_at);

-- Research snapshots are append-only by design; deduplication is out of scope.
CREATE TABLE IF NOT EXISTS questionnaire_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    vacancy_url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questionnaire_scans_detected_at
    ON questionnaire_scans(detected_at);

CREATE TABLE IF NOT EXISTS questionnaire_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES questionnaire_scans(id),
    body_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    is_radio INTEGER NOT NULL,
    options_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questionnaire_questions_scan_id
    ON questionnaire_questions(scan_id);

-- test_assignments — факт назначения внешнего теста работодателем (#180).
-- Отдельно от responses/actions: это событие чата, а не статус отклика и не
-- наше действие. Запись append-only, чтобы сохранять текст сообщения и URL.
-- topic — идентификатор конкретной переписки (см. responses.ResponseItem.topic):
-- одна вакансия может дать несколько чатов (повторный отклик тем же резюме
-- на ту же вакансию через разные топики), поэтому topic обязателен в ключе
-- дедупликации ниже — без него совпадающий текст сообщения из ДВУХ разных
-- чатов схлопнулся бы в одну запись и второе реальное событие терялось бы
-- безвозвратно под INSERT OR IGNORE.
CREATE TABLE IF NOT EXISTS test_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT,
    vacancy_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    employer TEXT NOT NULL,
    test_url TEXT NOT NULL,
    message_text TEXT NOT NULL,
    detected_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_test_assignments_detected_at
    ON test_assignments(detected_at);

-- Дедупликация: повторный обход responses --detect-external-tests читает то же
-- сообщение чата снова (детект read-only, без курсора по message_id) и без
-- этого индекса вставлял бы дубль строки при каждом запуске. Ключ включает
-- topic (не только vacancy_id), чтобы не схлопывать совпадающий текст из
-- разных переписок по одной вакансии; INSERT OR IGNORE делает повтор no-op.
CREATE UNIQUE INDEX IF NOT EXISTS idx_test_assignments_dedup
    ON test_assignments(topic, message_text);
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
    QUESTION_LOW_CONFIDENCE = "question_skipped_low_confidence"
    RESUME_VISIBILITY = "resume_visibility"  # отклик заблокирован видимостью резюме
    DUPLICATE = "duplicate"  # дубликат вакансии в одном сборе
    RELOCATION_NOT_ALLOWED = "relocation_not_allowed"
    DIRECT_APPLICATION = "direct_application"
    RESPONSE_REJECTED = "response_rejected"


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
    _SkipReasons.QUESTION_LOW_CONFIDENCE,
    _SkipReasons.RESUME_VISIBILITY,
    _SkipReasons.DUPLICATE,
    _SkipReasons.RELOCATION_NOT_ALLOWED,
    _SkipReasons.DIRECT_APPLICATION,
    _SkipReasons.RESPONSE_REJECTED,
)


#: Допустимые значения ``replies.status`` (#108, #201). ``uncertain`` означает,
#: что клик состоялся, но позитивный сигнал не был пойман за таймаут. Кортеж, не
#: set: порядок стабилен для сообщений об ошибке.
#:
#: Валидируется в record_reply намеренно (в отличие от record_action): опечатка
#: или синоним (``"SUCCESS"``, ``"sent"``) прошли бы в БД молча, has_replied
#: навсегда вернул бы False, и бот отправил бы работодателю ВТОРОЕ сообщение.
#: В actions такая же ошибка лишь искажает статистику, здесь — видна человеку.
#:
#: Асимметрия с :meth:`History.has_applied` (#176) намеренная: там ``uncertain``
#: дедуплицируется, потому что повторный отклик безопаснее пропустить, чем
#: отправить второй. Для ответа в чате ``uncertain`` пока НЕ дедуплицируется:
#: повтор может показать работодателю дублирующее сообщение, но дедупликация
#: навсегда оставила бы чат без ответа, если первое сообщение не дошло. Вопрос
#: должен быть пересмотрен по продакшен-статистике (#208), а не решён догадкой.
REPLY_STATUS_VALUES = ("success", "failed", "dry_run", "uncertain")


class History:
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
            conn.executescript(SCHEMA)
            _ensure_column(conn, "actions", "letter_variant", "TEXT")
            _ensure_column(conn, "actions", "search_query", "TEXT")
            _ensure_column(conn, "actions", "run_id", "TEXT")
            _ensure_column(conn, "actions", "reason_code", "TEXT")
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
            return row is not None

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

    def start_apply_run(self, *, command: str, requested_limit: int | None) -> str:
        """Recover abandoned apply runs and create a durable running row."""
        now = datetime.now().isoformat()
        run_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """UPDATE apply_runs SET status='orphaned', finished_at=?, exit_code=NULL,
                          detail=COALESCE(detail, 'recovered by next apply run')
                   WHERE status='running'""",
                (now,),
            )
            conn.execute(
                """INSERT INTO apply_runs
                   (run_id, command, requested_limit, status, started_at)
                   VALUES (?, ?, ?, 'running', ?)""",
                (run_id, command, requested_limit, now),
            )
        return run_id

    def finish_apply_run(
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
            raise ValueError(f"недопустимый статус apply run: {status}")
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE apply_runs SET status=?, attempted=?, success=?, failed=?,
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
                raise ValueError(f"running apply run не найден: {run_id}")

    def apply_runs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM apply_runs ORDER BY started_at")
            return [dict(row) for row in rows]

    def apply_run_action_counts(self, run_id: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT status, COUNT(*) AS count FROM actions
                   WHERE run_id=? AND action='apply' GROUP BY status""",
                (run_id,),
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

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
        """Finalize a pre-action audit marker without creating a second row."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE actions
                   SET status = ?, reason = ?, letter_variant = ?, reason_code = ?
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
            return datetime.fromisoformat(row["created_at"]) if row else None

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
            last_success = conn.execute(
                """
                SELECT created_at FROM actions
                WHERE resume_id = ? AND action = ? AND status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (resume_id, action),
            ).fetchone()
            params: tuple = (resume_id, action)
            since_clause = ""
            if last_success:
                since_clause = "AND created_at > ?"
                params = (*params, last_success["created_at"])
            row = conn.execute(
                f"""
                SELECT 1 FROM actions
                WHERE resume_id = ? AND action = ? AND status = 'uncertain' {since_clause}
                LIMIT 1
                """,
                params,
            ).fetchone()
            return row is not None

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

        Возвращает {"apply": {"success","dry_run","failed","uncertain"},
        "bump": {...}, "total"}. Пустой период → все нули. resume_id=None
        означает «по всем резюме».
        """
        result: dict = {
            "apply": {"success": 0, "dry_run": 0, "failed": 0, "uncertain": 0},
            "bump": {"success": 0, "dry_run": 0, "failed": 0, "uncertain": 0},
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

    def reply_summary(self, resume_id: str | None, period: str) -> dict:
        """Сводка наших ответов из локальной таблицы ``replies`` за период.

        Успешные ответы считаются отправленными; ``dry_run`` в отправки не
        входит. ``total`` и ``period``/``letter_variants`` уважают один и тот
        же ``period`` (как ``summary().total`` — #112 review), а не только
        ``resume_id``.
        """
        filters: list[str] = []
        params: list = []
        if resume_id is not None:
            filters.append("resume_id = ?")
            params.append(resume_id)
        since = self._period_since(period)
        period_filters = [*filters]
        period_params = [*params]
        if since is not None:
            period_filters.append("created_at >= ?")
            period_params.append(since)
        period_clause = " WHERE " + " AND ".join(period_filters) if period_filters else ""
        total_filters = [*period_filters, "status = 'success'"]
        total_clause = " WHERE " + " AND ".join(total_filters)
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM replies{total_clause}", period_params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT status, letter_variant, COUNT(*) AS cnt FROM replies{period_clause} "
                "GROUP BY status, letter_variant",
                period_params,
            ).fetchall()
        result = {
            "total": total,
            "period": {"success": 0, "failed": 0, "uncertain": 0},
            "letter_variants": {},
        }
        for row in rows:
            if row["status"] == "success":
                result["period"]["success"] += row["cnt"]
                variant = row["letter_variant"] or "unknown"
                result["letter_variants"][variant] = (
                    result["letter_variants"].get(variant, 0) + row["cnt"]
                )
            elif row["status"] == "failed":
                result["period"]["failed"] += row["cnt"]
            elif row["status"] == "uncertain":
                result["period"]["uncertain"] += row["cnt"]
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
        /applicant/negotiations общая, поэтому обход остаётся account-scope и
        ответ НЕ клонируется под все resume_id (это фабриковало бы данные).
        Однозначный SSR topic mapping может атрибутировать конкретную строку.
        Одна вакансия может дать
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
                       SET resume_id = COALESCE(?, resume_id), employer = ?,
                           last_status = status, status = ?,
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
                "UPDATE responses SET resume_id = COALESCE(?, resume_id), "
                "employer = ?, chat_url = ?, "
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

        Этапы КУМУЛЯТИВНЫЕ (sent ⊇ viewed ⊇ invited ⊇ replied ⊇ offer): вакансия,
        до которой дошло приглашение, считается и просмотренной; оффер — и
        просмотренным, и приглашённым, и отвеченным нами. Это необходимо, т.к.
        #12 хранит в responses только ТЕКУЩИЙ статус переписки (после
        read→invitation прежний read уже не виден) — некумулятивный подсчёт
        давал бы viewed=0 после перехода. «Просмотрено» = любой ответ
        работодателя (#12: read/response/invitation/discard/offer) — отказ или
        письмо тоже означают, что резюме видели. «Наш ответ» (replied, #112) =
        залогированный успешный ``replies``-ответ на invitation/offer, ИЛИ сам
        факт оффера (responses status='offer' или ручная пометка manual_offers)
        — оффер невозможен без нашего ответа, даже если сам факт ответа не
        попал в локальный журнал (ручной оффер, сбой логирования).

        Ответы берутся из responses (#12, account-scope по vacancy_id) и
        replies (#108, account-scope по topic) плюс липкие ручные пометки из
        manual_offers (per-resume). Группировка по actions.resume_id. Пер-резюме
        точность ограничена account-scope responses/replies (ответ одной
        вакансии зачтётся всем резюме, откликнувшимся в неё) — это ограничение
        источника данных #12/#108 (нет достоверного связывания ответ→резюме).

        Конверсии: view_rate=viewed/sent, invite_rate=invited/viewed, reply_rate=
        replied/invited, offer_rate=offer/invited; 0% при пустом знаменателе.
        Возвращает список словарей (по строке на resume_id, отсортированных по
        убыванию отправленных). Пусто → [].
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
                    ,COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        JOIN replies p ON p.topic = r.topic AND p.status = 'success'
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('invitation', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id AND r.status = 'offer'
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.vacancy_id END) AS replied
                FROM actions AS a
                {clause}
                GROUP BY a.resume_id
                ORDER BY sent DESC, a.resume_id
                """,
                params,
            ).fetchall()

        funnel: list[dict] = []
        for row in rows:
            sent, viewed, invited = row["sent"], row["viewed"], row["invited"]
            replied, offer = row["replied"], row["offer"]
            funnel.append(
                {
                    "resume_id": row["resume_id"],
                    "sent": sent,
                    "viewed": viewed,
                    "invited": invited,
                    "replied": replied,
                    "offer": offer,
                    "view_rate": self._pct(viewed, sent),
                    "invite_rate": self._pct(invited, viewed),
                    "reply_rate": self._pct(replied, invited),
                    "offer_rate": self._pct(offer, invited),
                }
            )
        return funnel

    def funnel_by_search_query(
        self,
        since: str | None = None,
        resume_id: str | None = None,
    ) -> list[dict]:
        """Воронка отправлено → оффер с группировкой по поисковому запросу.

        Атрибуция запроса (#420, PR #449) — сперва ``actions.search_query``,
        записанный в момент самого отклика (apply/run передают текущий
        ``resume.search.text``, approved-заявки — запрос, сохранённый в
        ``review_queue`` на момент постановки в очередь). Если он ``NULL``
        (строки, созданные до появления колонки — миграционное окно, а не
        дефект — см. #420: "не бэкафилить исторические actions"), запрос
        берётся через ``LEFT JOIN`` из ``vacancies_seen`` по ``vacancy_id`` —
        так отклик учитывается в каждом запросе, в котором была найдена его
        вакансия (``vacancies_seen`` допускает несколько таких строк). Внутри
        запроса счётчики дедуплицируются по паре (resume_id, vacancy_id), а не
        только по vacancy_id: ``idx_resume_vacancy_apply`` — UNIQUE по этой
        паре, поэтому два разных резюме легитимно откликаются на одну и ту же
        вакансию отдельными строками actions (code review #411) — дедуп по
        одному vacancy_id занижал бы sent/viewed/invited/offer/replied и искажал
        производные *_rate при дефолтном resume_id=None (все резюме). Этапы
        остаются кумулятивными, как в :meth:`funnel_by_resume`.
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

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(a.search_query, v.search_query) AS search_query,
                    COUNT(DISTINCT a.resume_id || ':' || a.vacancy_id) AS sent,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('read', 'response', 'invitation', 'discard', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.resume_id || ':' || a.vacancy_id END) AS viewed,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('invitation', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.resume_id || ':' || a.vacancy_id END) AS invited,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id AND r.status = 'offer'
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.resume_id || ':' || a.vacancy_id END) AS offer,
                    COUNT(DISTINCT CASE WHEN EXISTS (
                        SELECT 1 FROM responses r
                        JOIN replies p ON p.topic = r.topic AND p.status = 'success'
                        WHERE r.vacancy_id = a.vacancy_id
                          AND r.status IN ('invitation', 'offer')
                    ) OR EXISTS (
                        SELECT 1 FROM responses r
                        WHERE r.vacancy_id = a.vacancy_id AND r.status = 'offer'
                    ) OR EXISTS (
                        SELECT 1 FROM manual_offers m
                        WHERE m.resume_id = a.resume_id AND m.vacancy_id = a.vacancy_id
                    ) THEN a.resume_id || ':' || a.vacancy_id END) AS replied
                FROM actions AS a
                LEFT JOIN vacancies_seen AS v
                  ON v.vacancy_id = a.vacancy_id AND a.search_query IS NULL
                {clause}
                GROUP BY COALESCE(a.search_query, v.search_query)
                """,
                params,
            ).fetchall()

        funnel: list[dict] = []
        for row in rows:
            sent, viewed, invited = row["sent"], row["viewed"], row["invited"]
            replied, offer = row["replied"], row["offer"]
            funnel.append(
                {
                    "search_query": row["search_query"],
                    "sent": sent,
                    "viewed": viewed,
                    "invited": invited,
                    "replied": replied,
                    "offer": offer,
                    "view_rate": self._pct(viewed, sent),
                    "invite_rate": self._pct(invited, viewed),
                    "reply_rate": self._pct(replied, invited),
                    "offer_rate": self._pct(offer, invited),
                }
            )
        return sorted(
            funnel,
            key=lambda row: (
                -row["invite_rate"],
                -row["offer_rate"],
                row["search_query"] or "",
            ),
        )

    def count_unattributed_applies(
        self,
        since: str | None = None,
        resume_id: str | None = None,
    ) -> int:
        """Число успешных откликов без строки в ``vacancies_seen`` (code review #411).

        ``funnel_by_search_query`` INNER JOIN'ит ``actions`` к ``vacancies_seen``
        по ``vacancy_id`` — вакансии, которых там нет, молча выпадают из воронки.
        ``vacancies_seen`` заполняет только команда ``search`` (`upsert_vacancy_seen`
        вызывается из ``commands/search.py``); `apply`/`run` вызывают
        ``search_vacancies()`` напрямую и НЕ пишут в ``vacancies_seen`` — если
        пользователь откликался через `apply`/`run` без предварительного
        отдельного `search` по тем же вакансиям, эти отклики систематически не
        попадут в `funnel --search-query`. Используется командой `funnel` для
        `[INFO]`-предупреждения вместо тихой потери данных; не влияет на числа
        самой воронки.
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

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM actions AS a
                {clause}
                AND a.search_query IS NULL
                AND NOT EXISTS (
                    SELECT 1 FROM vacancies_seen AS v WHERE v.vacancy_id = a.vacancy_id
                )
                """,
                params,
            ).fetchone()
        return int(row["n"])

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
        employer_tier: str | None = None,
        vacancy_text: str | None = None,
        published_at: str | None = None,
    ) -> None:
        """Записывает/освежает карточку вакансии по (vacancy_id, search_query).

        Ключ UNIQUE(vacancy_id, search_query): одна вакансия по разным поисковым
        запросам — отдельные строки (рынок хочет видеть, по каким запросам что
        находится и за сколько). При повторном scrape та же пара обновляет
        title/company/salary (hh.ru мог поменять вилку) и двигает
        ``last_seen_at``; ``first_seen_at`` хранит ПЕРВОЕ появление и не трогается.

        Зарплата приходит из ``SalaryInfo`` (#34): ``salary_from``/``salary_to``
        оба NULL = «з/п не указана» (``parse_salary`` вернул None) — такая
        вакансия тоже пишется, для подсчёта доли рынка без зарплаты. Валюта НЕ
        нормализуется в одну: медиана считается в рамках одного search_query
        (внутри сферы валюта обычно однородна).

        ``employer_tier`` (#93) — уровень известности работодателя
        (``classify_employer``: top_tech/big_corp/mid/unknown). Записывается при
        сборе для группировки медианы в ``estimate_salary``. При обновлении
        существующей строки tier тоже освежается (компания могла накопить
        отзывов между scrape'ами; trusted-бейдж hh.ru на tier не влияет — #118).

        ``published_at`` — дата публикации вакансии на hh.ru, неизменна по
        своей природе. Селектор для неё опционален (см. ``selector_groups/
        search_page.py``), поэтому при повторном scrape без даты уже известное
        значение сохраняется (``COALESCE``), а не затирается NULL'ом.
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
                     salary_currency, search_query, first_seen_at,
                     last_seen_at, employer_tier, vacancy_text, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vacancy_id, search_query) DO UPDATE SET
                    title = excluded.title,
                    company = excluded.company,
                    salary_from = excluded.salary_from,
                    salary_to = excluded.salary_to,
                    salary_currency = excluded.salary_currency,
                    employer_tier = excluded.employer_tier,
                    vacancy_text = excluded.vacancy_text,
                    published_at = COALESCE(excluded.published_at, published_at),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    vacancy_id,
                    title,
                    company,
                    salary_from,
                    salary_to,
                    salary_currency,
                    search_query,
                    now,
                    now,
                    employer_tier,
                    vacancy_text,
                    published_at,
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
                "search_query, first_seen_at, last_seen_at, employer_tier, vacancy_text, "
                "published_at "
                "FROM vacancies_seen ORDER BY last_seen_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_vacancy_texts(self) -> list[str]:
        """Возвращает непустые тексты собранных вакансий для read-only отчётов."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT MAX(vacancy_text) AS vacancy_text FROM vacancies_seen "
                "WHERE vacancy_text IS NOT NULL AND vacancy_text != '' "
                "GROUP BY vacancy_id"
            ).fetchall()
        return [row["vacancy_text"] for row in rows]

    def vacancy_age_distribution(self, now: datetime | None = None) -> dict[str, int]:
        """Count observed vacancies by age of hh.ru publication date.

        UNIQUE-индекс — (vacancy_id, search_query), поэтому одна и та же
        вакансия, встреченная под несколькими поисковыми запросами, даёт
        несколько строк. Группируем по vacancy_id, чтобы посчитать каждую
        вакансию один раз (как list_vacancy_texts/estimate_salary).
        """
        now = now or datetime.now()
        result = {"<1 дня": 0, "1-7 дней": 0, "7-30 дней": 0, "30+ дней": 0, "неизвестно": 0}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT MAX(published_at) AS published_at FROM vacancies_seen GROUP BY vacancy_id"
            ).fetchall()
        for row in rows:
            value = row["published_at"]
            try:
                age = (now - datetime.fromisoformat(value)).total_seconds() / 86400
            except (TypeError, ValueError):
                result["неизвестно"] += 1
                continue
            if age < 1:
                result["<1 дня"] += 1
            elif age < 7:
                result["1-7 дней"] += 1
            elif age < 30:
                result["7-30 дней"] += 1
            else:
                result["30+ дней"] += 1
        return result

    # --- Профиль аккаунта для внешних форм (#282/#284) -----------------------

    def upsert_profile_field(self, question_key: str, value: str, source: str) -> None:
        """Сохраняет значение профиля, не смешивая источники.

        Ключ нормализуется тем же правилом, что и подписи полей внешних форм.
        Поэтому повторный login обновляет только ``hh_ru``-строку, а ручное
        значение для того же вопроса остаётся отдельной строкой.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_profile (question_key, value, source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(question_key, source) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (normalize(question_key), value, source, now),
            )

    def get_profile_answers(self) -> dict[str, str]:
        """Возвращает профиль для ``apply_answers`` с приоритетом manual."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT question_key, value
                FROM account_profile
                ORDER BY question_key,
                         CASE source WHEN 'manual' THEN 0 ELSE 1 END,
                         id
                """
            ).fetchall()
        answers: dict[str, str] = {}
        for row in rows:
            answers.setdefault(row["question_key"], row["value"])
        return answers

    def list_profile_fields(self) -> list[dict]:
        """Возвращает все исходные строки профиля для ``profile show``."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT question_key, value, source, updated_at
                FROM account_profile
                ORDER BY question_key, source, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_profile_field(self, question_key: str, source: str = "manual") -> bool:
        """Удаляет значение профиля указанного источника.

        Возвращает ``True``, если строка существовала. Нормализация ключа здесь
        повторяет ``upsert_profile_field`` и защищает вызывающих от расхождения
        между командами и автоматическим сбором профиля.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM account_profile WHERE question_key = ? AND source = ?",
                (normalize(question_key), source),
            )
        return cursor.rowcount > 0

    # --- Произвольные настройки CLI (#383) -----------------------------------

    def set_setting(self, key: str, value: str) -> None:
        """Создаёт или обновляет локальную настройку."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        """Возвращает настройку или ``None``, если ключ не найден."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def list_settings(self) -> list[dict[str, str]]:
        """Возвращает настройки в стабильном порядке ключей."""
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
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

        ``median_from`` может оказаться ВЫШЕ ``median_to`` — это не баг. Медианы
        считаются по разным подмножествам вакансий: если работодатели с высокими
        зарплатами публикуют «от 900 000» без потолка, а вилку целиком указывают
        те, кто платит меньше, нижняя медиана честно окажется выше верхней. Две
        цифры — это два независимых среза рынка, а не границы одного интервала.

        ``count`` = все собранные вакансии сферы, ``with_salary`` = сколько с
        ЛЮБОЙ указанной границей (покрытие: вакансия «от N» — это данные, а не
        пропуск). ``low_sample`` = True, если реальных ЗП меньше
        ``_LOW_SAMPLE_N`` — такие сферы сортируются ниже надёжных.

        Сортировка: сначала надёжные сферы по убыванию медианы, затем ненадёжные
        (тоже по убыванию) — выгодные направления наверху, но не ценой того, что
        лидером станет строка на двух вакансиях. Ранжирует ``median_to``, а при
        её отсутствии — ``median_from`` (см. :meth:`_rank_median`).

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
                # low_sample — про сферу целиком (есть ли вообще на что смотреть),
                # поэтому считается по with_salary (покрытие по любой границе).
                # Надёжность КАЖДОЙ из двух медиан по отдельности этим флагом не
                # передать: у них независимые выборки, и бывает with_from=10 при
                # with_to=1. Для этого случая guard — n прямо в ячейке
                # (report_market._format_median), а не флаг строки.
                entry["low_sample"] = (entry["with_salary"] or 0) < self._LOW_SAMPLE_N
                out.append(entry)
            # Сортировка в Python (а не в SQL): при include_estimates медиана
            # меняется уже после SELECT, а low_sample — производное поле. Ключ:
            # надёжные сферы выше ненадёжных, внутри группы — по убыванию
            # ранжирующей медианы, тай-брейк по count и search_query.
            out.sort(
                key=lambda e: (
                    e["low_sample"],
                    -self._rank_median(e),
                    -e["count"],
                    e["search_query"],
                )
            )
            return out

    @staticmethod
    def _rank_median(entry: dict) -> int:
        """По какой медиане ранжировать сферу: верхняя, иначе нижняя.

        Цель #66 — потолок предложения, поэтому основной ключ ``median_to``. Но
        у сферы, где ВСЕ вакансии «от N», верхней медианы не существует, и голый
        ``median_to`` дал бы ей 0 — она упала бы в конец списка, даже если её
        нижние границы выше чужих верхних. Это инверсия ровно того сравнения,
        ради которого отчёт и считается, поэтому при отсутствии верхней медианы
        ранжируем по нижней. Сравнение «нижняя против верхней» неточное, но
        честнее, чем считать отсутствие данных нулевым доходом.
        """
        return entry["median_to"] or entry["median_from"] or 0

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
        # Доминирующая валюта сферы (#122) — фильтр для ВСЕХ выборок ниже:
        # и реальных ЗП, и кандидатов на оценку, и самих tier-медиан.
        currency = entry.get("currency")
        # Реальные salary_to сферы (уже есть) + оценки для вакансий без ЗП.
        # #122: только доминирующая валюта — та же, по которой посчитана медиана
        # в market_salary_by_query, иначе оценки вернули бы смешение валют назад.
        real = [
            r["salary_to"]
            for r in conn.execute(
                "SELECT salary_to FROM vacancies_seen "
                "WHERE search_query = ? AND salary_to IS NOT NULL "
                "AND salary_currency IS ?",
                [query, currency],
            ).fetchall()
        ]
        # Кандидаты на оценку — вакансии БЕЗ ОБЕИХ границ (#125). Раньше отбор шёл
        # по `salary_to IS NULL`, из-за чего реальная вакансия «от 900 000»
        # считалась «без ЗП» и получала выдуманный потолок — то самое
        # достраивание вилки, которое запрещено дизайн-решением #125. Побочный
        # эффект был нагляден: нижняя медиана коридора могла оказаться ВЫШЕ
        # верхней. Теперь «от N» — это данные, и оценивать там нечего.
        #
        # #122: вакансия с ЗП в НЕдоминирующей валюте тоже не кандидат. Она уже
        # исключена из медианы и посчитана в other_currency, о чём отчёт прямо
        # пишет «не вошли в медиану»; вернуть её через оценку — сделать сноску
        # ложью. А вот у вакансии совсем без ЗП валюты нет по определению
        # (salary_currency IS NULL), и фильтровать её по валюте нельзя — иначе
        # оценивать будет вообще нечего, ради чего #93 и заводился. Поэтому
        # условие на валюту здесь не нужно: строки без обеих границ по
        # построению вне валютного разделения.
        no_salary_tiers = [
            r["employer_tier"]
            for r in conn.execute(
                "SELECT employer_tier FROM vacancies_seen "
                "WHERE search_query = ? AND salary_to IS NULL AND salary_from IS NULL",
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

    def list_skipped(self, reason: str | None = None) -> list[dict]:
        """Возвращает журнал отсева с данными вакансий, свежие первыми.

        ``vacancies_seen`` может содержать несколько строк одной вакансии (по
        разным поисковым запросам), поэтому JOIN агрегирует её до одной строки
        на запись ``skipped`` и не дублирует результаты команды.
        ``LEFT JOIN`` сохраняет старые записи отсева, для которых карточка ещё
        не была сохранена.
        """
        where = "WHERE s.reason = ?" if reason is not None else ""
        params = (reason,) if reason is not None else ()
        with self._connect() as conn:
            rows = conn.execute(
                "WITH latest_vacancy AS ("
                "SELECT vacancy_id, title, company FROM ("
                "SELECT v.*, ROW_NUMBER() OVER ("
                "PARTITION BY vacancy_id ORDER BY last_seen_at DESC, id DESC"
                ") AS rn FROM vacancies_seen v"
                ") WHERE rn = 1"
                "), seen_queries AS ("
                "SELECT vacancy_id, GROUP_CONCAT(DISTINCT search_query) AS search_query "
                "FROM vacancies_seen GROUP BY vacancy_id"
                ") SELECT s.created_at, s.resume_id, s.vacancy_id, s.reason, "
                "v.title, v.company, q.search_query "
                "FROM skipped s LEFT JOIN latest_vacancy v "
                "ON v.vacancy_id = s.vacancy_id LEFT JOIN seen_queries q "
                "ON q.vacancy_id = s.vacancy_id "
                f"{where} "
                "ORDER BY s.created_at DESC, s.id DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

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
    # IGNORE), а dry_run/failed/uncertain ключ не занимают и копятся для аналитики.
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
        — из :data:`REPLY_STATUS_VALUES`; ``uncertain`` означает, что клик был
        выполнен, но подтверждение не поймано за таймаут.

        Идемпотентность — по partial-UNIQUE, то есть только по УСПЕШНЫМ ответам:
        повторный ``success`` на ту же (topic, inbound_marker) — no-op (INSERT OR
        IGNORE), первая успешная запись не перезаписывается. Неуспешные попытки
        (``dry_run``/``failed``/``uncertain``) ключ не занимают: они копятся строками для
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

        Только ``status='success'``: ``dry_run``, ``failed`` и ``uncertain`` отправкой не
        считаются. В частности, ``uncertain`` намеренно НЕ дедуплицирует ответ,
        в отличие от :meth:`has_applied` (#176): повтор может показать работодателю
        дублирующее сообщение, но дедупликация оставила бы чат без ответа, если
        первое сообщение не дошло. Это компромисс до накопления продакшен-статистики
        (#208). ``dry_run`` также не дедуплицирует отклик: иначе холостой прогон
        навсегда заблокировал бы боевой ответ на живое входящее.

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

    def reply_candidates(self, limit: int | None = None) -> list[dict]:
        """Return account-wide chat candidates using only local history.

        The live message marker is intentionally not available here.  It is
        read only after a candidate chat is opened, where ``has_replied`` can
        perform the final duplicate check.
        """
        sql = """
            SELECT r.vacancy_id, r.topic, COALESCE(v.title, r.vacancy_id) AS title,
                   COALESCE(r.employer, '') AS employer
              FROM responses AS r
              LEFT JOIN vacancies_seen AS v ON v.vacancy_id = r.vacancy_id
             WHERE r.topic IS NOT NULL
             GROUP BY r.vacancy_id, r.topic
             ORDER BY MAX(r.last_seen_at) DESC, r.id DESC
        """
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def mark_robot_questionnaire(
        self, topic: str, *, vacancy_id: str | None = None, reason: str = "detected"
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO robot_questionnaires "
                "(topic, vacancy_id, reason, detected_at) VALUES (?, ?, ?, ?)",
                (topic, vacancy_id, reason, datetime.now().isoformat()),
            )

    def record_questionnaire(
        self,
        resume_id: str,
        vacancy_id: str,
        vacancy_url: str,
        title: str,
        company: str,
        questions: list[dict[str, object]],
    ) -> None:
        """Append a questionnaire snapshot and all visible question options."""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO questionnaire_scans
                   (resume_id, vacancy_id, vacancy_url, title, company, detected_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (resume_id, vacancy_id, vacancy_url, title, company, datetime.now().isoformat()),
            )
            scan_id = cursor.lastrowid
            conn.executemany(
                """INSERT INTO questionnaire_questions
                   (scan_id, body_index, text, kind, is_radio, options_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        scan_id,
                        int(question["body_index"]),
                        str(question["text"]),
                        str(question["kind"]),
                        int(bool(question["is_radio"])),
                        json.dumps(question["options"], ensure_ascii=False),
                    )
                    for question in questions
                ],
            )

    def is_robot_questionnaire(self, topic: str) -> bool:
        with self._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM robot_questionnaires WHERE topic = ?", (topic,)
                ).fetchone()
                is not None
            )

    def list_robot_questionnaires(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT topic, vacancy_id, reason, detected_at FROM robot_questionnaires "
                    "ORDER BY detected_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]

    def record_reply_and_action(
        self,
        topic: str,
        inbound_marker: str,
        *,
        vacancy_id: str,
        resume_id: str | None = None,
        status: str,
        reason: str | None = None,
        letter_variant: str | None = None,
    ) -> None:
        """Write the reply journal and action audit in one SQLite transaction.

        ``resume_id`` — резюме, с которого шёл отклик, из SSR ``topicList[].resumeId``
        (#200). Опционален: hh.ru отдаёт его стабильно (проверено 2026-08-16, 7/7
        переписок), но дрейф разметки не должен ронять журналирование ответа —
        отсутствие даёт NULL в ``replies`` и account-wide сентинел в ``actions``,
        как было до #200.
        """
        if status not in REPLY_STATUS_VALUES:
            raise ValueError(f"недопустимый status={status!r} для replies")
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO replies
                   (topic, inbound_marker, vacancy_id, resume_id, status,
                    letter_variant, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    topic,
                    inbound_marker,
                    vacancy_id,
                    resume_id,
                    status,
                    letter_variant,
                    reason,
                    now,
                ),
            )
            # actions.resume_id is NOT NULL — пустая строка остаётся сентинелом
            # только когда SSR не отдал resumeId (см. докстринг).
            conn.execute(
                """INSERT INTO actions
                   (resume_id, vacancy_id, action, status, reason, letter_variant, created_at)
                   VALUES (?, ?, 'reply', ?, ?, ?, ?)""",
                (resume_id or "", vacancy_id, status, reason, letter_variant, now),
            )

    def replies_since(self, since: datetime) -> list[dict]:
        """Наши ответы, записанные после ``since`` — для аналитики и отчётов.

        Свежие первыми. Возвращает ВСЕ статусы (включая ``dry_run``/``failed``/
        ``uncertain``):
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

    # --- Назначения внешних тестов (#180) -----------------------------------

    def record_test_assigned(
        self,
        resume_id: str | None,
        vacancy_id: str,
        topic: str,
        employer: str,
        test_url: str,
        message_text: str,
        *,
        detected_at: datetime | None = None,
    ) -> None:
        """Append a read-only fact discovered in an employer chat.

        resume_id is account-scope metadata, not a verified attribution
        (/applicant/negotiations does not expose which resume a chat belongs
        to) — callers must pass None unless a real mapping exists.

        OR IGNORE: a re-run of ``responses --detect-external-tests`` re-reads
        the same chat message (no message_id cursor), so without dedup it
        would insert a duplicate row every time — see
        ``idx_test_assignments_dedup``.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO test_assignments
                    (resume_id, vacancy_id, topic, employer, test_url, message_text, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resume_id,
                    vacancy_id,
                    topic,
                    employer,
                    test_url,
                    message_text,
                    (detected_at or datetime.now()).isoformat(),
                ),
            )

    def test_assignments_since(self, since: datetime) -> list[dict]:
        """Return detected test assignments newer than ``since``, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT resume_id, vacancy_id, topic, employer, test_url, message_text, detected_at
                FROM test_assignments
                WHERE detected_at > ?
                ORDER BY detected_at DESC, id DESC
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


_APPLY_INDEX_SQL = (
    "CREATE UNIQUE INDEX idx_resume_vacancy_apply "
    "ON actions(resume_id, vacancy_id) "
    "WHERE action = 'apply' AND status IN ('success', 'uncertain')"
)


def _purge_legacy_dry_run_applied_skips(conn: sqlite3.Connection) -> None:
    """Remove stale ALREADY_APPLIED skips created solely by old dry-runs.

    Before #431, apply dry-runs wrote an ``actions(status='dry_run')`` row and
    ``filter_candidates`` subsequently cached ``ALREADY_APPLIED`` in
    ``skipped``. The latter cache is checked before ``has_applied()``, so
    changing deduplication alone would leave those vacancies blocked forever.
    Rows without that exact legacy signature are intentionally untouched: they
    may represent a real site-side duplicate detection or another skip cause.
    The set-based ``EXCEPT`` keeps this one-time cleanup from doing a
    correlated actions-table scan for every skipped row.
    """
    conn.execute(
        """
        DELETE FROM skipped
        WHERE reason = 'already_applied'
          AND (resume_id, vacancy_id) IN (
                SELECT resume_id, vacancy_id
                FROM actions
                WHERE action = 'apply' AND status = 'dry_run'
                EXCEPT
                SELECT resume_id, vacancy_id
                FROM actions
                WHERE action = 'apply' AND status IN ('success', 'uncertain')
          )
        """
    )


def _ensure_apply_index(conn: sqlite3.Connection) -> None:
    """Идемпотентно доводит idx_resume_vacancy_apply до актуального условия (#177).

    CREATE UNIQUE INDEX IF NOT EXISTS не пересоздаст индекс с новым WHERE на
    уже существующей БД (тот же caveat #51, что и у _ensure_column) — старые
    базы содержат индекс без 'uncertain' в условии. Как и _ensure_column,
    сначала читаем текущее определение из sqlite_master и трогаем индекс
    ТОЛЬКО если оно отличается — иначе каждый CLI-вызов делал бы лишний
    DROP+CREATE под write/schema-lock (cycle-review #177, round 2).

    'uncertain' появился в PR #176 (уже в main) ДО этого индекс-фикса, поэтому
    на реальных установках уже могли накопиться дубли (resume_id, vacancy_id)
    со статусом 'uncertain' под старым (более узким) индексом — CREATE UNIQUE
    INDEX на них упадёт IntegrityError и History() будет ронять вообще все
    команды бота. Явно проверяем дубли ПЕРЕД пересозданием: если они есть —
    не создаём индекс и логируем warning, оставляя дедупликацию на чистой
    Python-логике has_applied() (SELECT, не зависит от индекса) до ручной
    чистки БД администратором — это безопаснее, чем падать намертво.
    """
    current = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_resume_vacancy_apply'"
    ).fetchone()
    if current is not None and current[0] == _APPLY_INDEX_SQL:
        return

    dupes = conn.execute(
        "SELECT resume_id, vacancy_id, COUNT(*) c FROM actions "
        "WHERE action = 'apply' AND status IN ('success', 'uncertain') "
        "GROUP BY resume_id, vacancy_id HAVING c > 1"
    ).fetchall()
    if dupes:
        # #177 round 3 (Codex): старый индекс НЕ трогаем, если пересборку
        # выполнить нельзя — раньше DROP выполнялся безусловно ДО этой
        # проверки, снимая DB-уровня UNIQUE-защиту целиком (включая для
        # success пар, которые старый индекс ещё покрывал), даже
        # если дубли есть только среди новых 'uncertain' записей.
        logger.warning(
            "idx_resume_vacancy_apply не пересоздан: найдено %d пар "
            "(resume_id, vacancy_id) с дублирующимися apply-записями "
            "(success/uncertain). UNIQUE constraint на них упал бы "
            "с IntegrityError. Дедупликация продолжает работать через "
            "has_applied(), но без обновлённой DB-уровня защиты для "
            "'uncertain' — почистите дубли в actions вручную.",
            len(dupes),
        )
        return
    conn.execute("DROP INDEX IF EXISTS idx_resume_vacancy_apply")
    conn.execute(_APPLY_INDEX_SQL)

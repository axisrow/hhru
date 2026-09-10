"""Схема SQLite и упорядоченные миграции (#1035).

Извлечено из ``history.py`` механически: ровно тот же DDL и те же функции
миграции, которые раньше жилы в одном модуле с ``History``. Порядок применения
критичен и остаётся в ``History._init_schema`` (см. ``history.py``): RENAME и
пересборка competitor-таблиц ДО ``executescript(SCHEMA)``, доводка колонок и
индексов ПОСЛЕ. Системы миграций в проекте нет намеренно (оверинжиниринг) —
новые таблицы дописываются в SCHEMA, новые колонки — идемпотентными
``_ensure_column``.
"""

from __future__ import annotations

import logging
import sqlite3

from .market_schema import MARKET_TABLES_DDL

logger = logging.getLogger("hhru_bot.history")

# Схема SQLite — одна константа, CREATE TABLE IF NOT EXISTS для всех таблиц.
# Системы миграций для такого маленького проекта не нужно (оверинжиниринг): при
# сильных изменениях схемы базу пересоздают заново (данных мало). _init_schema()
# применяет SCHEMA идемпотентно при каждом открытии — IF NOT EXISTS гарантирует,
# что повторный запуск на существующей базе не падает и не трогает данные.
_SCHEMA_HEAD = """\
CREATE TABLE IF NOT EXISTS reply_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL, inbound_marker TEXT NOT NULL,
    vacancy_id TEXT NOT NULL, resume_id TEXT,
    message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_drafts_topic_marker
    ON reply_drafts(topic, inbound_marker);
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

-- #461: изначально apply_runs (PR #460, только apply); переименована в
-- command_runs, так как ledger применим к любой durable-команде, не только
-- apply. Миграция старых БД — идемпотентный ALTER TABLE RENAME в
-- _rename_apply_runs_to_command_runs (_init_schema), CREATE TABLE IF NOT
-- EXISTS здесь покрывает свежую БД без старой apply_runs. Второй таблицы и
-- алиасов старых имён функций намеренно нет (один пользователь, одна БД).
CREATE TABLE IF NOT EXISTS command_runs (
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
    detail TEXT,
    owner_pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_command_runs_status ON command_runs(status, started_at);

-- Runtime selector observations (#701).  This is deliberately separate from
-- the provenance catalog: the catalog describes what a selector is, while
-- these rows record what the browser actually observed during one healthcheck.
CREATE TABLE IF NOT EXISTS selector_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    logical_id TEXT NOT NULL,
    status TEXT NOT NULL,
    found INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE (run_id, logical_id)
);
CREATE INDEX IF NOT EXISTS idx_selector_observations_run_id
    ON selector_observations(run_id, id);

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
    last_invitation_at TEXT,
    chat_url TEXT,
    response_date TEXT,
    last_seen_at TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (vacancy_id, topic)
);

-- Confirmed applications made outside this tool (manual/external).  This is
-- deliberately not actions: it must affect deduplication without inflating
-- our apply counters or pretending that we own the run.
CREATE TABLE IF NOT EXISTS external_applied (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    origin TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (resume_id, vacancy_id, topic)
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
"""

# Рыночные таблицы (vacancies_seen + competitor_*, #1106) живут отдельной константой
# MARKET_TABLES_DDL в market_schema.py — их создаёт и общая рыночная база
# data/market.db (MarketStore), а здесь они остаются частью per-account history.db
# (легаси-данные, доктрина «ничего не удалять»: новые таблицы history.db не читает).

_SCHEMA_TAIL = """\
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

CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('company','keyword','vacancy')),
    value TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(entry_type, value)
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

-- #robot-reply: резолв очереди робот-анкет — колонкой (resolved_at), не
-- DELETE: таблица append-only, факт обнаружения — часть аудита. NULL = в
-- очереди; комментарий ДО CREATE — комментарии в теле мешают DROP COLUMN.
CREATE TABLE IF NOT EXISTS robot_questionnaires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL UNIQUE,
    vacancy_id TEXT,
    reason TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    answer TEXT
);
CREATE INDEX IF NOT EXISTS idx_robot_questionnaires_detected_at
    ON robot_questionnaires(detected_at);

-- Вердикт пользователя «робот или человек» для topic. Приоритетнее ЛЮБОЙ
-- эвристики детекта (лейбл/вопросы/скорость): автоматический классификатор
-- доверчив, спроектированный текст его обманывает, истина фиксируется
-- человеком. Машина вердикт не перезаписывает (только robot-mark --clear).
CREATE TABLE IF NOT EXISTS robot_verdicts (
    topic TEXT PRIMARY KEY,
    verdict TEXT NOT NULL CHECK (verdict IN ('robot', 'human')),
    annotated_at TEXT NOT NULL
);

-- Research snapshots are append-only by design; deduplication is out of scope.
CREATE TABLE IF NOT EXISTS questionnaire_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    vacancy_url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'probe',
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
    options_json TEXT NOT NULL,
    answer TEXT,
    answer_source TEXT,
    confidence REAL,
    filled INTEGER NOT NULL DEFAULT 0,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_questionnaire_questions_scan_id
    ON questionnaire_questions(scan_id);

-- questionnaire_templates (#482) — как отвечать на вопрос данного смысла.
-- resume_id='' означает ответ уровня АККАУНТА, непустой — переопределение для
-- конкретного резюме (приоритет резюме над аккаунтом — get_questionnaire_templates).
-- NOT NULL DEFAULT '' вместо nullable намеренно: SQLite не считает два NULL
-- одинаковыми, поэтому UNIQUE с nullable resume_id допустил бы неограниченное
-- число дублирующих account-строк для одного шаблона.
CREATE TABLE IF NOT EXISTS questionnaire_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template TEXT NOT NULL,
    resume_id TEXT NOT NULL DEFAULT '',
    cluster TEXT NOT NULL DEFAULT 'mixed',
    mode TEXT NOT NULL CHECK (mode IN ('static', 'contextual')),
    answer TEXT,
    instruction TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (template, resume_id)
);

-- questionnaire_examples (#482) — подтверждённые пользователем формулировки.
-- Двойное назначение: (1) few-shot примеры в contextual-промпте, (2) корпус
-- сопоставлений «формулировка -> шаблон», который issue просит накапливать для
-- будущего классического ML. question_key = normalize(текст вопроса), поэтому
-- phrase-стратегия резолвера — один индексированный lookup.
CREATE TABLE IF NOT EXISTS questionnaire_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template TEXT NOT NULL,
    resume_id TEXT NOT NULL DEFAULT '',
    question_key TEXT NOT NULL,
    question_text TEXT NOT NULL,
    confirmed_by TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    UNIQUE (template, resume_id, question_key)
);
CREATE INDEX IF NOT EXISTS idx_questionnaire_examples_key
    ON questionnaire_examples(question_key);

-- questionnaire_pending (#482) — очередь вопросов, на которые бот не имеет
-- права ответить сам (нет шаблона, низкая уверенность, комплаенс без явного
-- значения). Зеркалит review_queue: status + индекс по (status, id).
-- UNIQUE(resume_id, question_key) + ON CONFLICT DO UPDATE: один и тот же
-- вопрос встречается у десятков работодателей, и без ключа дедупликации
-- очередь заполнялась бы копиями одной строки быстрее, чем её успевают разобрать.
CREATE TABLE IF NOT EXISTS questionnaire_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL DEFAULT '',
    vacancy_url TEXT NOT NULL DEFAULT '',
    question_key TEXT NOT NULL,
    question_text TEXT NOT NULL,
    kind TEXT NOT NULL,
    is_radio INTEGER NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '[]',
    template TEXT,
    cluster TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (resume_id, question_key)
);
CREATE INDEX IF NOT EXISTS idx_questionnaire_pending_status
    ON questionnaire_pending(status, id);

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

# Личные таблицы + рыночные: per-account history.db по-прежнему создаёт ВСЕ
# таблицы (легаси-данные в старых базах и joins аналитики на vacancies_seen
# остаются валидными), новая запись competitor-таблиц идёт в data/market.db.
SCHEMA = _SCHEMA_HEAD + MARKET_TABLES_DDL + _SCHEMA_TAIL

#: Провенанс режима сессии у строк членства, записанных до #669: он там не
#: хранился, а `--auth-mode authenticated` уже существовал, поэтому подставить
#: 'anonymous' значило бы выдумать провенанс. Такие строки не попадают ни в
#: один scoped-отчёт и видны только в общем — тот же приём, что и NULL-режим
#: у legacy-строк `competitor_collection_runs`.
LEGACY_UNKNOWN_SCOPE = "unknown"


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


def _rename_apply_runs_to_command_runs(conn: sqlite3.Connection) -> None:
    """Идемпотентно переименовывает apply_runs → command_runs (#461).

    ALTER TABLE ... RENAME TO переносит и старый индекс idx_apply_runs_status
    под старым именем — SQLite не переименовывает индексы автоматически при
    RENAME TABLE, поэтому индекс пересоздаём отдельно под новым именем.
    Без второй таблицы и без wrapper-алиасов старых имён: один пользователь,
    одна БД, миграция выполняется один раз на старой установке и затем
    становится no-op (apply_runs больше не существует).
    """
    exists_old = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='apply_runs'"
    ).fetchone()
    if not exists_old:
        return
    exists_new = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='command_runs'"
    ).fetchone()
    if exists_new:
        # Обе таблицы существуют одновременно — не должно происходить при
        # нормальной эксплуатации (RENAME атомарно устраняет apply_runs).
        # Оставляем command_runs как источник истины и не трогаем данные.
        return
    conn.execute("ALTER TABLE apply_runs RENAME TO command_runs")
    conn.execute("DROP INDEX IF EXISTS idx_apply_runs_status")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_command_runs_status ON command_runs(status, started_at)"
    )


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


def _migrate_competitor_query_scope_schema(
    conn: sqlite3.Connection, *, _fail_after_create: bool = False
) -> None:
    """Re-key resume membership by the full search scope (#669).

    ``search_in`` for legacy rows is a FACT: before ``--search-in`` existed
    ``pos`` was hardcoded ``full_text``, so no other value was reachable.
    ``auth_mode`` is NOT the same case -- ``--auth-mode authenticated`` predates
    this migration, the mode was user-selectable, and membership never recorded
    it. Labelling those rows ``anonymous`` would invent provenance, so they are
    marked ``LEGACY_UNKNOWN_SCOPE`` instead: matching neither scoped report,
    visible only in the unscoped one, mirroring how
    ``competitor_collection_runs`` already treats legacy runs of unknown auth
    scope. A NULL would say the same thing but silently break the composite
    PRIMARY KEY -- SQLite treats every NULL as distinct, so re-running a legacy
    row would insert a duplicate instead of conflicting. Reconstructing the mode
    from timestamps is not an option either: ``last_seen_at`` moves on every
    re-scrape, so a resume seen under both modes carries an interval spanning
    both.

    SQLite cannot alter a PRIMARY KEY in place, so the table is rebuilt the same
    way ``_migrate_competitor_skills_schema`` does -- but inside an explicit
    transaction. DDL does not join the connection's implicit transaction, so a
    crash between the RENAME and the copy would otherwise leave an empty new
    table beside the renamed legacy one; the guard below would then see
    ``search_in`` in that empty table and skip the migration forever, silently
    dropping every membership row from scoped reports.

    CAVEAT: a database collected between the ``--search-in`` flag landing and
    this migration holds both populations under one ``search_query`` with no
    surviving provenance -- every membership row there is labelled
    ``full_text``. That is the honest floor, not a repair: the scope was never
    recorded, so it cannot be recovered. Such a mixed row set stays mixed under
    ``--search-in full_text``; a clean per-scope population comes from
    re-collecting under the wanted scope, which then writes its own rows.
    """
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("competitor_resume_queries",),
    ).fetchone()
    if not table or "search_in" in (table[0] or ""):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "ALTER TABLE competitor_resume_queries RENAME TO competitor_resume_queries_legacy"
        )
        conn.execute("DROP INDEX IF EXISTS idx_competitor_queries_query")
        conn.execute("""CREATE TABLE competitor_resume_queries (
            resume_id TEXT NOT NULL,
            search_query TEXT NOT NULL,
            search_in TEXT NOT NULL DEFAULT 'full_text',
            auth_mode TEXT NOT NULL DEFAULT 'unknown',
            search_rank INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (resume_id, search_query, search_in, auth_mode)
        )""")
        if _fail_after_create:
            raise sqlite3.OperationalError("simulated crash during copy")
        conn.execute(
            """INSERT OR IGNORE INTO competitor_resume_queries
               (resume_id, search_query, search_in, auth_mode,
                search_rank, first_seen_at, last_seen_at)
               SELECT resume_id, search_query, 'full_text', ?,
                      search_rank, first_seen_at, last_seen_at
               FROM competitor_resume_queries_legacy""",
            (LEGACY_UNKNOWN_SCOPE,),
        )
        conn.execute("DROP TABLE competitor_resume_queries_legacy")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_competitor_queries_query
               ON competitor_resume_queries(search_query, search_in, auth_mode, search_rank)"""
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _migrate_competitor_skills_schema(
    conn: sqlite3.Connection, *, _fail_after_create: bool = False
) -> None:
    """Remove legacy skill filters that could roll back a complete resume.

    Wrapped in an explicit transaction for the same reason as
    ``_migrate_competitor_query_scope_schema``: DDL does not join the
    connection's implicit transaction, so a crash between the RENAME and the
    copy strands every skill row in the renamed legacy table -- and the guard
    below then sees a CHECK-free table and skips the migration forever.
    """
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("competitor_resume_skills",),
    ).fetchone()
    conn.execute("DROP TRIGGER IF EXISTS competitor_resume_skills_no_contacts")
    conn.execute("DROP TRIGGER IF EXISTS competitor_resume_skills_no_contacts_update")
    if not table or "CHECK" not in (table[0] or "").upper():
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "ALTER TABLE competitor_resume_skills RENAME TO competitor_resume_skills_legacy"
        )
        conn.execute("""CREATE TABLE competitor_resume_skills (
            resume_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            proficiency TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (resume_id, skill)
        )""")
        if _fail_after_create:
            raise sqlite3.OperationalError("simulated crash during skills copy")
        rows = conn.execute(
            """SELECT resume_id, skill, proficiency, first_seen_at, last_seen_at
               FROM competitor_resume_skills_legacy"""
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO competitor_resume_skills VALUES (?, ?, ?, ?, ?)", tuple(row)
            )
        conn.execute("DROP TABLE competitor_resume_skills_legacy")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

"""Схема общей рыночной базы data/market.db (#1106).

Рынок один на все аккаунты: резюме и история откликов — личные (per-account
``data/accounts/<name>/history.db``, доктрина #706), а собранные данные о
рынке (конкуренты #578 + вакансии #66) — общие. DDL рыночных таблиц живёт
здесь единственной константой ``MARKET_TABLES_DDL``; с #1109 она компонует
ТОЛЬКО ``market_schema.SCHEMA`` (те же таблицы плюс служебная ``market_meta``
с маркером одноразовой миграции) — в SCHEMA per-account history.db рыночные
таблицы больше не входят, свежесоздаваемые history.db их не создают, а
аналитика читает рынок через MarketStore. Системы миграций нет намеренно
(тот же принцип, что и у history_schema): новые таблицы дописываются в
константу, новые колонки — идемпотентными ``_ensure_column`` у владельца
стоража.
"""

from __future__ import annotations

# DDL рыночных таблиц — дословно тот же текст, что раньше жил внутри
# history_schema.SCHEMA (#1106); с #1109 единственное место их создания —
# data/market.db (MarketStore), весь код читает/пишет их только там.
MARKET_TABLES_DDL = """\
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
    -- Доп. признаки карточки для статистики/ML (#517, приоритет-1 из #516):
    -- город/адрес, метка удалённой работы, категория опыта, структурные
    -- сниппеты "Требования"/"Обязанности". Все опциональны — NULL/0, если
    -- hh.ru не отдал блок в разметке карточки (тот же паттерн, что
    -- employer_tier/vacancy_text/published_at).
    address TEXT,
    is_remote INTEGER,
    experience TEXT,
    snippet_requirement TEXT,
    snippet_responsibility TEXT,
    -- Приоритет-2 из #516: опциональные бейджи "Подработка" и
    -- "Можно без резюме". NULL означает отсутствие наблюдения.
    side_job INTEGER,
    no_resume INTEGER,
    -- Приоритет-3 из #551: редкие признаки карточки. metro_stations — JSON
    -- массив строк, так как на карточке может быть несколько станций.
    activity TEXT,
    hh_rating TEXT,
    hrbrand_winner INTEGER,
    metro_stations TEXT,
    UNIQUE (vacancy_id, search_query)
);

-- competitor_resumes — текущие профессиональные снимки чужих резюме (#578).
CREATE TABLE IF NOT EXISTS competitor_resumes (
    resume_id TEXT PRIMARY KEY,
    resume_url TEXT NOT NULL,
    desired_role TEXT NOT NULL,
    area TEXT,
    relocation TEXT,
    business_trips TEXT,
    metro_station TEXT,
    salary_from INTEGER,
    salary_to INTEGER,
    salary_currency TEXT,
    experience_months INTEGER,
    specializations TEXT NOT NULL DEFAULT '[]',
    employment_types TEXT NOT NULL DEFAULT '[]',
    work_formats TEXT NOT NULL DEFAULT '[]',
    languages TEXT NOT NULL DEFAULT '[]',
    education TEXT NOT NULL DEFAULT '[]',
    experience_summary TEXT,
    achievements TEXT,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competitor_resume_skills (
    resume_id TEXT NOT NULL,
    skill TEXT NOT NULL,
    proficiency TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (resume_id, skill)
);

-- Членство резюме в выдаче ключуется полной идентичностью выборки, а не
-- одним текстом запроса (#669). `search_in` и `auth_mode` меняют не точность
-- одной и той же популяции, а то, КАКАЯ популяция собрана: «AI» в режиме
-- full_text даёт ~5000 резюме с ~81% графических дизайнеров (`.ai` — формат
-- Adobe Illustrator в навыках), а position — 619 профильных. Без них в ключе
-- отчёт по одному `--text` молча склеивал бы обе выборки, а общий
-- search_rank перезаписывался бы более поздним прогоном.
CREATE TABLE IF NOT EXISTS competitor_resume_queries (
    resume_id TEXT NOT NULL,
    search_query TEXT NOT NULL,
    search_in TEXT NOT NULL DEFAULT 'full_text',
    -- 'unknown' (LEGACY_UNKNOWN_SCOPE), а не 'anonymous': режим сессии был
    -- выбираемым до #669 и в членстве не записывался, поэтому у легаси-строк
    -- он неизвестен, а не анонимен. NOT NULL — потому что NULL в составном
    -- PRIMARY KEY не конфликтует сам с собой и ломал бы дедупликацию.
    auth_mode TEXT NOT NULL DEFAULT 'unknown',
    search_rank INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (resume_id, search_query, search_in, auth_mode)
);
CREATE INDEX IF NOT EXISTS idx_competitor_queries_query
    ON competitor_resume_queries(search_query, search_in, auth_mode, search_rank);

CREATE TABLE IF NOT EXISTS competitor_collection_runs (
    run_id TEXT PRIMARY KEY,
    search_query TEXT NOT NULL,
    auth_mode TEXT,
    search_in TEXT,
    max_pages INTEGER NOT NULL,
    requested_page_size INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    cards_seen INTEGER NOT NULL DEFAULT 0,
    details_saved INTEGER NOT NULL DEFAULT 0,
    details_failed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT,
    owner_pid INTEGER,
    heartbeat_at TEXT,
    last_started_page INTEGER,
    last_completed_page INTEGER,
    resume_page INTEGER,
    resumed_from_run_id TEXT,
    observed_page_size INTEGER,
    exit_code INTEGER,
    cards_seen_completed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_competitor_runs_query
    ON competitor_collection_runs(search_query, started_at);
"""

# market_meta — служебная таблица market.db: ключ-значение для маркеров
# одноразовой миграции из per-account history.db (#1106).
SCHEMA = (
    MARKET_TABLES_DDL
    + """\
CREATE TABLE IF NOT EXISTS market_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
)

# Маркер выполненной миграции данных из history.db в market.db: его наличие
# делает повторное открытие no-op (копирование идемпотентно, но гонять его на
# каждом открытии незачем).
MIGRATION_MARKER_KEY = "migrated_from_history"

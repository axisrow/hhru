-- 012_responses.sql — мониторинг ответов работодателей (#12, Этап 2).
-- Конвенция: номер миграции = номер ишью (012 = #12).

-- Одна строка НА ПЕРЕПИСКУ (account-scope): текущий «свежий» статус ответа
-- работодателя, перезаписываемый при каждом fetch_responses (upsert). В отличие
-- от actions (append-only журнал откликов/поднятий), здесь хранится последнее
-- состояние переписки, а не история переходов — для дашборда «что нового».
--
-- Почему account-scope (НЕ на пару resume_id+vacancy_id): страница
-- /applicant/negotiations общая по аккаунту, и карточка переписки НЕ несёт
-- достоверного признака «какому резюме принадлежит ответ». Клонирование одного
-- ответа под все resume_id из конфига фабриковало бы данные (ответ резюме A
-- приписывался бы и резюме B).
--
-- Ключ UNIQUE — (vacancy_id, topic): одна вакансия может дать НЕСКОЛЬКО
-- переписок (напр. отклик с разных резюме), каждая со своим topic (id чата из
-- chat_url ?topic=...). Ключ по вакансии один затирал бы соседние переписки и
-- терял их chat_url/статус. topic=NULL (ответ без чата) группируется по
-- vacancy_id — UNIQUE допускает несколько NULL (стандартный SQLite), так что
-- безтопиковые ответы разных вакансий не коллидируют, а одной вакансии с topic
-- и без — две разные строки (корректно: «приглашение в чат» и «отказ без чата»).
--
-- Поля (схема из ишью #12):
--   status        — текущий статус (invitation|discard|response|read).
--   last_status   — ПРЕДЫДУЩИЙ статус (до последней смены); на момент смены в него
--                   копируется прежний status, чтобы видеть «откуда → куда» перешёл
--                   ответ (read→invitation). NULL, пока смен статуса не было.
--   created_at    — когда строка впервые заведена (первое появление ответа =
--                   «first_seen_at» из ишью).
--   status_changed_at — когда сменился status (upsert двигает только при смене).
--   last_seen_at  — когда последний раз видели на /applicant/negotiations
--                   (освежается при каждом обходе, даже без смены статуса).
--   response_date — исходная дата ответа с hh.ru (как текст с карточки; парсинг
--                   конкретной даты не делается — форматы hh.ru зависят от локали,
--                   как raw_date в VacancyCard, и для дашборда «что нового»
--                   достаточно текстовой метки).
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

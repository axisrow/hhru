-- 012_responses.sql — мониторинг ответов работодателей (#12, Этап 2).
-- Конвенция: номер миграции = номер ишью (012 = #12).

-- Одна строка НА ВАКАНСИЮ (account-scope): текущий «свежий» статус ответа
-- работодателя, перезаписываемый при каждом fetch_responses (upsert). В отличие
-- от actions (append-only журнал откликов/поднятий), здесь хранится последнее
-- состояние переписки, а не история переходов — для дашборда «что нового».
--
-- Почему account-scope (НЕ на пару resume_id+vacancy_id): страница
-- /applicant/negotiations общая по аккаунту, и карточка переписки НЕ несёт
-- достоверного признака «какому резюме принадлежит ответ». Клонирование одного
-- ответа под все resume_id из конфига фабриковало бы данные (ответ резюме A
-- приписывался бы и резюме B). Поэтому responses хранятся в один экземпляре на
-- вакансию; resume_id оставлен опциональной колонкой под будущую достоверную
-- атрибуцию (напр. через cross-check с actions, где известен (resume_id,
-- vacancy_id) отклика), но в UNIQUE не входит.
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
    employer TEXT,
    status TEXT NOT NULL,
    last_status TEXT,
    chat_url TEXT,
    response_date TEXT,
    last_seen_at TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (vacancy_id)
);

CREATE INDEX IF NOT EXISTS idx_responses_status_changed_at
    ON responses(status_changed_at);

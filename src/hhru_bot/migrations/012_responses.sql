-- 012_responses.sql — мониторинг ответов работодателей (#12, Этап 2).
-- Конвенция: номер миграции = номер ишью (012 = #12).

-- Одна строка на пару (resume_id, vacancy_id): текущий «свежий» статус ответа
-- работодателя, перезаписываемый при каждом fetch_responses (upsert). В отличие
-- от actions (append-only журнал откликов/поднятий), здесь хранится последнее
-- состояние переписки, а не история переходов — для дашборда «что нового».
CREATE TABLE IF NOT EXISTS responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    employer TEXT,
    status TEXT NOT NULL,           -- invitation|discard|response|read (см. responses.ResponseStatus)
    chat_url TEXT,                  -- /applicant/negotiations?... ссылка на чат с работодателем
    last_seen_at TEXT NOT NULL,     -- когда последний раз видели на /applicant/negotiations
    status_changed_at TEXT NOT NULL,-- когда сменился status (upsert обновляет только при смене)
    created_at TEXT NOT NULL,       -- когда строка впервые заведена (первое появление ответа)
    UNIQUE (resume_id, vacancy_id)
);

CREATE INDEX IF NOT EXISTS idx_responses_status_changed_at
    ON responses(status_changed_at);

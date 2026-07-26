-- 001_actions.sql — базовая схема истории откликов/поднятий резюме.
-- Перенесено дословно из history.SCHEMA. Номер миграции = 001 (базовая).
-- Конвенция: будущие миграции именуются по номеру ишью (002 = #12, 017 = #17).

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

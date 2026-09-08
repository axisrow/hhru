"""Профиль: настройки, ответы анкеты профиля, blacklist и skipped (#1035).

Выделено из ``history.py`` механически.
"""

from __future__ import annotations

from datetime import datetime

from .external_forms.detect import normalize
from .history_skip_reasons import SKIP_REASONS


class ProfileMixin:
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

    def add_blacklist(self, entry_type: str, value: str, reason: str, created_by: str) -> None:
        from .blacklist import normalize_value, validate_value

        value = normalize_value(value)
        validate_value(entry_type, value)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO blacklist(entry_type,value,reason,created_by,created_at) "
                "VALUES (?,?,?,?,?)",
                (entry_type, value, reason.strip(), created_by.strip(), datetime.now().isoformat()),
            )

    def remove_blacklist(self, entry_type: str, value: str) -> int:
        from .blacklist import normalize_value

        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM blacklist WHERE entry_type=? AND value=?",
                (entry_type, normalize_value(value)),
            )
            conn.execute("DELETE FROM skipped WHERE reason=?", (SKIP_REASONS.BLACKLIST,))
            return cur.rowcount

    def list_blacklist(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM blacklist ORDER BY id")]

    def blacklist_sets(self) -> dict[str, set[str]]:
        return {
            kind: {row["value"] for row in self.list_blacklist() if row["entry_type"] == kind}
            for kind in ("company", "keyword", "vacancy")
        }

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

"""MarketStore — общая рыночная база data/market.db (#1106).

Рынок один на все аккаунты: конкурент-снимки (#578) и собранные вакансии
(#66) — факты о рынке hh.ru, а не личная история, поэтому они живут в
отдельной cwd-относительной базе ``data/market.db`` (дефолт — по образцу
``cli.DEFAULT_HISTORY_PATH``), которую ``competitors collect/report`` и
``market`` открывают ВСЕГДА, независимо от ``--account``/``default_account``.
Личное (actions, лимиты, дедуп, анкеты) остаётся в per-account history.db —
доктрина #706 не меняется.

Тонкий стораж: повторно использует миксины ``History`` (Competitors/Vacancies/
Analytics) как есть — им нужен только ``_connect``/``db_path``. Отдельные
рыночные методы не дублируются. При первом открытии выполняется одноразовая
миграция КОПИРОВАНИЕМ из корневой data/history.db и всех
data/accounts/*/history.db (маркер в market_meta); старые таблицы в history.db
не трогаются и не удаляются — правило проекта «ничего не удалять» абсолютное.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .history_analytics import AnalyticsMixin
from .history_competitors import CompetitorsMixin, ensure_competitor_norm_columns
from .history_vacancies import VacanciesMixin
from .market_norm import fold_key, split_roles
from .market_schema import BACKFILL_MARKER_KEY, MIGRATION_MARKER_KEY, SCHEMA

logger = logging.getLogger("hhru_bot.market")

# Дефолт — ОТНОСИТЕЛЬНЫЙ (relative-to-cwd), тот же принцип, что у
# cli.DEFAULT_HISTORY_PATH: после `pip install` пакет уезжает в site-packages,
# привязка к расположению кода ломала бы поиск data/. Читается в рантайме
# (не в сигнатуре по умолчанию), чтобы тесты могли подменить константу.
DEFAULT_MARKET_PATH = Path("data") / "market.db"

# Таблицы, переезжающие из per-account history.db при первой миграции.
_MIGRATION_TABLES = (
    "competitor_resumes",
    "competitor_resume_skills",
    "competitor_resume_queries",
    "competitor_collection_runs",
    "vacancies_seen",
)


class MarketStore(AnalyticsMixin, VacanciesMixin, CompetitorsMixin):
    """Хранилище общей рыночной базы; методы — унаследованные миксины History."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_MARKET_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            ensure_competitor_norm_columns(conn)
            self._migrate_from_history(conn)
            self._backfill_normalized_keys(conn)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _history_sources(self) -> list[Path]:
        """Источники миграции: корневой history.db и все аккаунтовые, рядом с market.db.

        Пути считаем от родителя market.db, а не от cwd: тесты открывают базу
        во временном каталоге, и искать данные надо рядом с ней, а не в cwd.
        """
        parent = self.db_path.parent
        sources = [parent / "history.db"]
        accounts_dir = parent / "accounts"
        if accounts_dir.is_dir():
            sources.extend(sorted(p for p in accounts_dir.glob("*/history.db") if p.is_file()))
        return [s for s in sources if s.is_file() and s.resolve() != self.db_path.resolve()]

    def _migrate_from_history(self, conn: sqlite3.Connection) -> None:
        """Одноразовое копирование рыночных таблиц из history.db (#1106).

        КОПИРОВАНИЕМ, не переносом: источники не мутируются вовсе (правило
        «ничего не удалять»). Идемпотентно (INSERT OR IGNORE по тем же
        PK/UNIQUE-ключам), но после успешного прохода маркер в market_meta
        снимает работу с последующих открытий. Колонки берутся пересечением
        PRAGMA table_info — легаси-источник может не иметь поздних колонок.

        При конфликте ключа между источниками побеждает ПЕРВЫЙ в порядке
        ``_history_sources`` (корневой history.db, потом аккаунты по алфавиту),
        а не самый свежий срез по last_seen_at/updated_at — осознанный выбор
        для одноразовой миграции: реальная свежесть приезжает первым же
        collect/search после неё, а гоняться за timestamp'ами между источниками
        ради строк, которые вот-вот перезапишет штатный upsert, незачем.
        """
        done = conn.execute(
            "SELECT value FROM market_meta WHERE key = ?", (MIGRATION_MARKER_KEY,)
        ).fetchone()
        if done is not None:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            for source_path in self._history_sources():
                self._copy_market_tables(conn, source_path)
            conn.execute(
                "INSERT OR REPLACE INTO market_meta (key, value) VALUES (?, ?)",
                (MIGRATION_MARKER_KEY, datetime.now().isoformat(timespec="seconds")),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def backfill_normalized_keys(self) -> None:
        """Публичная обёртка бэкфилла: тесты и ручной перезапуск (сняв маркер)."""
        with self._connect() as conn:
            self._backfill_normalized_keys(conn)

    def _backfill_normalized_keys(self, conn: sqlite3.Connection) -> None:
        """Одноразовое заполнение фолд-ключей и таблицы ролей (маркер в market_meta).

        Автостарт из __init__ — тот же паттерн, что _migrate_from_history:
        без него SQL-рецепты по *_key (docs/market-recipes.md) видели бы NULL
        до ручного скрипта. Одна BEGIN IMMEDIATE: крах в любой точке =
        ROLLBACK без маркера = чистый перезапуск при следующем открытии.
        Идёт ПОСЛЕ _migrate_from_history: скопированные из легаси-источников
        строки (с NULL-ключами) попадают в этот же проход.
        """
        done = conn.execute(
            "SELECT value FROM market_meta WHERE key = ?", (BACKFILL_MARKER_KEY,)
        ).fetchone()
        if done is not None:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Повторяющихся имён на порядок больше, чем уникальных: один кэш
            # на проход вместо сотен тысяч повторных fold_key (~360k строк).
            cache: dict[str, str] = {}

            def cached_key(value: str) -> str | None:
                key = cache.get(value)
                if key is None:
                    key = fold_key(value)
                    cache[value] = key
                return key or None

            resumes = conn.execute(
                "SELECT resume_id, desired_role FROM competitor_resumes"
            ).fetchall()
            conn.executemany(
                "UPDATE competitor_resumes SET desired_role_key=? WHERE resume_id=?",
                ((cached_key(str(row["desired_role"])), row["resume_id"]) for row in resumes),
            )
            skills = conn.execute(
                "SELECT resume_id, skill FROM competitor_resume_skills"
            ).fetchall()
            conn.executemany(
                "UPDATE competitor_resume_skills SET skill_key=? WHERE resume_id=? AND skill=?",
                ((cached_key(str(row["skill"])), row["resume_id"], row["skill"]) for row in skills),
            )
            queries = conn.execute(
                """SELECT resume_id, search_query, search_in, auth_mode
                   FROM competitor_resume_queries"""
            ).fetchall()
            conn.executemany(
                """UPDATE competitor_resume_queries SET search_query_key=?
                   WHERE resume_id=? AND search_query=? AND search_in=? AND auth_mode=?""",
                (
                    (
                        cached_key(str(row["search_query"])),
                        row["resume_id"],
                        row["search_query"],
                        row["search_in"],
                        row["auth_mode"],
                    )
                    for row in queries
                ),
            )
            # Роли пересобираются с нуля напрямую, без по-строчного
            # SELECT/DELETE _rebuild_competitor_roles: маркера нет — таблица
            # пуста (прошлый запуск откатился), first_seen сохранять нечего,
            # а ~30k пустых SELECT+DELETE — секунды чистого waste.
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("DELETE FROM competitor_resume_roles")
            for row in resumes:
                for position, part in enumerate(split_roles(str(row["desired_role"]))):
                    conn.execute(
                        """INSERT OR IGNORE INTO competitor_resume_roles
                           (resume_id, role, role_key, is_primary, first_seen_at, last_seen_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            row["resume_id"],
                            part,
                            cached_key(part),
                            1 if position == 0 else 0,
                            now,
                            now,
                        ),
                    )
            conn.execute(
                "INSERT OR REPLACE INTO market_meta (key, value) VALUES (?, ?)",
                (BACKFILL_MARKER_KEY, now),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _copy_market_tables(self, conn: sqlite3.Connection, source_path: Path) -> None:
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            for table in _MIGRATION_TABLES:
                src_cols = {
                    row["name"] for row in source.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not src_cols:
                    continue  # в этом history.db таблицы нет — нечего копировать
                dst_cols = {
                    row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                common = [c for c in dst_cols if c in src_cols]
                if not common:
                    continue
                columns = ", ".join(common)
                rows = source.execute(f"SELECT {columns} FROM {table}").fetchall()
                if not rows:
                    continue
                conn.executemany(
                    f"INSERT OR IGNORE INTO {table} ({columns}) VALUES "
                    f"({', '.join('?' for _ in common)})",
                    [tuple(row) for row in rows],
                )
                logger.info(
                    "market.db: мигрировано %d строк %s из %s", len(rows), table, source_path
                )
        finally:
            source.close()


def open_market() -> MarketStore | None:
    """Открывает общую рыночную базу; None — мягкая деградация (#1106/#1109).

    Единая точка открытия для команд (search/funnel/skipped/adaptive-report/
    reply-employers/learn): невозможность открыть market.db не должна валить
    команду — личная история пишется и анализируется независимо, карточные
    поля (title/зарплата/запрос) при этом честно пустуют. Вызывающий передаёт
    полученный инстанс (или None) в методы аналитики параметром — глобального
    синглтона нет (Wave 0).
    """
    try:
        return MarketStore()
    except Exception as e:  # noqa: BLE001 — рынок не должен валить команду
        logger.warning("Не открыть общую рыночную базу market.db: %s", e)
        return None

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
from .history_competitors import CompetitorsMixin
from .history_vacancies import VacanciesMixin
from .market_schema import MIGRATION_MARKER_KEY, SCHEMA

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
            self._migrate_from_history(conn)

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

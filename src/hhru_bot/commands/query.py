"""Команда query: произвольный read-only SELECT к локальной history.db (#45).

Образец — s3rgeym/hh-applicant-tool (из аудита #21): позиционный SQL, --csv,
-o <file>, интерактивный режим без аргументов.

read-only — КРИТИЧНО (CLAUDE.md: history.db пользователь меняет только через
бот). Тройная защита:

1. Префиксный guard: принимаем только SELECT / WITH ... SELECT. Любой
   INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/PRAGMA-запись → понятный отказ и
   exit(1). Регулярка НЕ парсит SQL целиком (хрупко) — только проверяет, что
   инструкция начинается с разрешённого ключевого слова. НЕ единственная
   защита: ``WITH ... DELETE/UPDATE/INSERT`` тоже начинается с WITH — их
   блокирует слой 3.
2. Соединение открывается в режиме SQLite URI ``?mode=ro``, причём URI
   строится через ``Path.as_uri()`` (percent-encoding). Наивная f-строка
   ``file:{path}?mode=ro`` позволяла caller-у подложить ``?mode=rw`` в путь и
   переопределить режим — закодированный URI этого не даёт.
3. ``PRAGMA query_only=ON`` — независимый слой на стороне движка: запрещает
   любые мутации (включая ``WITH ... DELETE``) независимо от режима
   соединения и валидности URI. Это последний бастиер, если URI как-то
   пропустит rw.

Вывод — ASCII-таблица через переиспользуемый ``report._ascii_table`` (НЕ
дублируем форматтер). При --csv — чистый CSV без украшательств (csv.writer).
Только текст/ASCII — НИКАКИХ эмодзи (правило проекта: CLI-вывод чистый).
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sqlite3
import sys
from pathlib import Path

from ..report import _ascii_table

# Ключевые слова, с которых может начинаться read-only запрос.
# Регистронезависимо, допустимы лидирующие пробелы/переносы.
# WITH ... SELECT (CTE) разрешён — это по-прежнему SELECT-семантика.
_READONLY_STMT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

# Ключевые слова мутирующих инструкций (для понятного сообщения об отказе).
# Используется только для диагностики — настоящую защиту даёт mode=ro.
_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE", "PRAGMA")

# SQLite companion-файлы главного history.db (WAL + shared-memory + rollback
# journal). -o в любой из них перезаписал бы живой companion во время
# конкурентной записи бота — запрещаем в _output_is_history.
_SQLITE_COMPANION_SUFFIXES = ("-wal", "-shm", "-journal")


class QueryError(Exception):
    """Ошибка запроса: не-SELECT, синтаксис, отсутствие БД. Печатается в stderr."""


def _ensure_schema(db_path: Path) -> None:
    """Гарантирует, что файл history.db существует и схема применена.

    Свежий пользователь может запустить ``query`` до любого действия бота —
    тогда файла нет, и read-only соединение не сможет его создать. Один раз
    открываем через History (он применит миграции), после чего переходим в
    read-only режим. Само наличие БД — это тоже часть контракта «меняется
    только через бот»: миграции применяются движком бота, а не произвольным SQL.
    """
    if db_path.exists():
        return
    # Ленивый импорт — разрывает цикл (history импортирует migrations и т.д.).
    from ..history import History

    History(db_path)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Открывает соединение в режиме read-only (URI ?mode=ro) + query_only.

    URI строится через ``Path.as_uri()`` (percent-encoding), а НЕ f-строкой:
    иначе caller мог подставить ``?mode=rw`` в путь history и переопределить
    режим. ``?mode=ro`` дописывается к уже закодированному URI.

    Дополнительно включается ``PRAGMA query_only=ON`` — независимый слой,
    блокирующий мутации на уровне движка даже при гипотетическом обходе URI.
    """
    resolved = db_path.resolve()
    uri = resolved.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    # Третий слой защиты: движок запрещает запись независимо от режима
    # соединения. PRAGMA read-only по intent (запись настроек), но здесь мы
    # её ВКЛЮЧАЕМ как защиту — это не user-SQL, а наш собственный setup.
    conn.execute("PRAGMA query_only=ON")
    return conn


def _check_readonly(sql: str) -> None:
    """Префиксный guard: только SELECT / WITH. Иначе QueryError с понятным текстом."""
    if not _READONLY_STMT_RE.match(sql):
        head = sql.strip().split(None, 1)[0] if sql.strip() else "<пусто>"
        kind = head.upper()
        write_hint = " (запись)" if kind in _WRITE_KEYWORDS else ""
        raise QueryError(
            "Допускаются только read-only запросы (SELECT / WITH ... SELECT). "
            f"history.db меняется только через бот.{write_hint} "
            "INSERT/UPDATE/DELETE/DROP/ALTER/CREATE запрещены."
        )


def execute(sql: str, db_path: str | Path, *, csv: bool = False) -> str:
    """Исполняет один SELECT и возвращает отформатированный результат.

    csv=False → ASCII-таблица (через report._ascii_table).
    csv=True  → чистый CSV (csv.writer, экранирование по RFC 4180).

    Бросает QueryError при не-SELECT или проблемах с БД; sqlite3.Error при
    синтаксической ошибке SQL пробрасывается наверх (run() ловит и печатает).
    """
    db_path = Path(db_path)
    _check_readonly(sql)
    _ensure_schema(db_path)

    try:
        conn = _connect_readonly(db_path)
    except sqlite3.OperationalError as e:
        raise QueryError(f"Не удалось открыть history.db (read-only): {e}") from e

    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        # Пустой результат: fetchall=[]. Достаём имена колонок из
        # cursor.description, чтобы всё равно рисовать шапку (как stats).
        col_names = _column_names_from_description(cur.description)
        rows_str: list[list[str]] = []
    else:
        col_names = rows[0].keys()
        rows_str = [[_cell(v) for v in row] for row in rows]

    return _format_result(list(col_names), rows_str, as_csv=csv)


def _format_result(header: list[str], rows: list[list[str]], *, as_csv: bool) -> str:
    """Отрисовка результата: CSV (чистый) или ASCII-таблица (через report)."""
    if as_csv:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(header)
        writer.writerows(rows)
        return out.getvalue().rstrip("\n")
    return _ascii_table(header, rows)


def _column_names_from_description(description) -> list[str]:
    """Имена колонок из cursor.description (для пустого результата).

    SQLite возвращает описание только после execute; для SELECT без строк оно
    всё равно заполнено именами колонок.
    """
    if not description:
        return []
    return [d[0] for d in description]


def _cell(value) -> str:
    """Приведение значения ячейки к строке: None → '' (пусто), как в report."""
    if value is None:
        return ""
    return str(value)


def _resolve_output_target(output: str | Path) -> Path:
    """Резолвит цель -o в абсолютный путь (для сверки с history)."""
    return Path(output).resolve()


def _output_is_history(output_resolved: Path, history_path: str | Path) -> bool:
    """True, если цель -o указывает на history.db или его SQLite companion-файлы.

    Регрессия (Codex cycle-1): ``query ... -o data/history.db`` перезаписывал
    SQLite-файл ASCII-текстом → необратимая потеря истории.
    Регрессия (Codex cycle-2): ``-o data/history.db-wal`` (или ``-shm`` /
    ``-journal``) перезаписывал companion-файлы SQLite — во время конкурентной
    записи бота committed-транзакции могут жить в WAL/-journal до checkpoint,
    перезапись теряет данные или калечит БД.
    Регрессия (Codex cycle-3): case-вариант пути (``history.db-WAL`` на
    case-insensitive FS) и hard link на companion имеют тот же inode, но
    другой путь — строковое сравнение их пропускало.

    Запрещаем: главный путь и каждый companion-кандидат, сверяя И путь
    (case-folded, для несуществующих целей), И inode (samefile, для
    существующих alias'ов — symlink/hard-link/case-variant).
    """
    history_resolved = Path(history_path).resolve()
    # Кандидаты на «нельзя перезаписывать»: сама БД + companion-файлы.
    companion_candidates = [history_resolved] + [
        Path(str(history_resolved) + sfx) for sfx in _SQLITE_COMPANION_SUFFIXES
    ]
    out_norm = str(output_resolved).casefold()
    for cand in companion_candidates:
        # 1. Совпадение пути case-insensitively — ловит и несуществующую цель
        #    (например будущий history.db-WAL на case-insensitive FS).
        if out_norm == str(cand).casefold():
            return True
        # 2. Совпадение inode — ловит существующий alias (hard link, symlink,
        #    case-variant того же файла). samefile требует существования обеих
        #    сторон; для отсутствующей цели просто пропускает (п.1 уже покрыл).
        try:
            if output_resolved.samefile(cand):
                return True
        except OSError:
            continue
    return False


def _write_output(text: str, output: str | Path, history_path: str | Path) -> None:
    """Пишет результат в файл -o с проверками. Бросает QueryError при отказе."""
    output_resolved = _resolve_output_target(output)
    if _output_is_history(output_resolved, history_path):
        raise QueryError(
            f"Отказ: -o указывает на саму history.db ({output_resolved}). "
            "Запись результата в базу уничтожит историю — выберите другой файл."
        )
    try:
        output_resolved.write_text(text + "\n", encoding="utf-8")
    except OSError as e:
        # Понятная ошибка вместо необработанного traceback (несуществующий
        # каталог, нет прав и т.п.) — единый стиль с ошибками SQL/guard.
        raise QueryError(f"Не удалось записать результат в {output_resolved}: {e}") from e


def _emit(text: str, args: argparse.Namespace) -> None:
    """Куда писать результат: -o <file> (с защитой от перезаписи history) или stdout."""
    if args.output:
        _write_output(text, args.output, args.history)
    else:
        print(text)


def run(args: argparse.Namespace) -> None:
    """Точка входа команды query. Исполняет SELECT и печатает результат.

    Без позиционного SQL — интерактивный режим (опционально, отложен до
    follow-up: сейчас выводим подсказку и выходим с кодом 2).
    """
    if not args.sql:
        print(
            "Интерактивный режим пока не реализован. Передайте SQL позиционно: "
            'hhru-bot query "SELECT ..."',
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        result = execute(args.sql, args.history, csv=args.csv)
        _emit(result, args)
    except QueryError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"Ошибка SQL: {e}", file=sys.stderr)
        sys.exit(1)


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "query",
        help="Произвольный read-only SELECT к локальной history.db",
        description=(
            "Исполнить SELECT к history.db и вывести ASCII-таблицу или CSV. "
            "Только read-only (SELECT/WITH) — история меняется только через бот."
        ),
    )
    p.add_argument("sql", nargs="?", help="SQL-запрос (SELECT / WITH ... SELECT)")
    p.add_argument("--csv", action="store_true", help="Вывести CSV вместо ASCII-таблицы")
    p.add_argument("-o", dest="output", help="Записать результат в файл вместо stdout")
    p.set_defaults(func=run)

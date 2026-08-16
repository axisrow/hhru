"""Тесты команды query (#45): произвольный SELECT к локальной history.db.

Браузер не нужен — только SQLite. Покрываем:
- ASCII-вывод (через report._ascii_table, без дублирования) и --csv;
- read-only guard: отказ на INSERT/UPDATE/DELETE/DROP/ALTER/CREATE с понятным
  сообщением, код возврата 1, БД не изменена;
- -o <file>: результат пишется в файл, не в stdout;
- tmp_path history (seeded через History.record_action);
- register()/--help и присутствие флагов (характеризация структуры argparse).

CLAUDE.md: history.db пользователь меняет только через бот — query обязан быть
строго read-only.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hhru_bot.commands import query as query_cmd
from hhru_bot.history import History

pytestmark = pytest.mark.integration

# --- хелперы -----------------------------------------------------------------


def _seed_history(tmp_path):
    """Создаёт history.db с парой записей и возвращает путь к ней."""
    h = History(tmp_path / "h.db")
    h.record_action("r1", "v1", "apply", "success")
    h.record_action("r1", "v2", "apply", "failed", "captcha")
    return tmp_path / "h.db"


def _args(history_path, sql=None, **overrides) -> argparse.Namespace:
    base = {
        "history": str(history_path),
        "sql": sql,
        "csv": False,
        "output": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --- ASCII-вывод -------------------------------------------------------------


def test_query_ascii_table_default(capsys, tmp_path):
    """Без --csv — ASCII-таблица с рамкой (+---+/| col |), переиспользует
    report._ascii_table (нет дублирования форматтера)."""
    db = _seed_history(tmp_path)

    query_cmd.run(_args(db, sql="SELECT COUNT(*) AS n FROM actions"))

    out = capsys.readouterr().out
    # рамка ASCII-таблицы
    assert "+" in out and "-" in out and "|" in out
    # алиас колонки из SELECT отражён в шапке
    assert "n" in out
    # две записи — число 2 присутствует в теле
    assert " 2 " in out or out.strip().endswith("2 |") or "| 2" in out


def test_query_ascii_shows_column_names(capsys, tmp_path):
    """Шапка = имена колонок из SELECT, строки — значения."""
    db = _seed_history(tmp_path)

    query_cmd.run(_args(db, sql="SELECT vacancy_id FROM actions ORDER BY vacancy_id"))

    out = capsys.readouterr().out
    assert "vacancy_id" in out  # шапка
    assert "v1" in out and "v2" in out  # значения


def test_query_empty_result_still_renders_header(capsys, tmp_path):
    """Пустой результат всё равно рисует шапку (как report.format_actions)."""
    db = _seed_history(tmp_path)

    query_cmd.run(_args(db, sql="SELECT vacancy_id FROM actions WHERE vacancy_id = 'nope'"))

    out = capsys.readouterr().out
    assert "vacancy_id" in out  # шапка есть
    # нет строк-значений: v1/v2 не должны попасть в тело
    lines = [ln for ln in out.splitlines() if "v1" in ln or "v2" in ln]
    assert lines == []


# --- CSV ---------------------------------------------------------------------


def test_query_csv_machine_readable(capsys, tmp_path):
    """--csv — чистый CSV: первая строка = имена колонок, без рамок/пробелов."""
    db = _seed_history(tmp_path)

    query_cmd.run(
        _args(db, sql="SELECT vacancy_id, status FROM actions ORDER BY vacancy_id", csv=True)
    )

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "vacancy_id,status"
    assert "v1,success" in lines
    assert "v2,failed" in lines
    # нет ASCII-украшений
    assert "+" not in out
    assert "|" not in out


def test_query_csv_quoting(tmp_path):
    """CSV корректно экранирует запятую/кавычку в значениях (csv.writer)."""
    db = tmp_path / "h.db"
    h = History(db)
    # reason с запятой — типичный «captcha, retry»
    h.record_action("r1", "v1", "apply", "failed", "captcha, retry")

    rows = query_cmd.execute("SELECT reason FROM actions", db, csv=True)
    assert '"captcha, retry"' in rows


# --- read-only guard ---------------------------------------------------------


# Прямые мутирующие инструкции — guard отсекает их ещё на префиксе (понятное
# сообщение о запрете SELECT-only).
_DIRECT_WRITE_STMTS = [
    "INSERT INTO actions (resume_id) VALUES ('x')",
    "UPDATE actions SET status = 'success'",
    "DELETE FROM actions",
    "DROP TABLE actions",
    "ALTER TABLE actions ADD COLUMN x TEXT",
    "CREATE TABLE evil (id INTEGER)",
]

# WITH-обёртки над мутациями — префикс начинается с WITH (guard их пропускает),
# поэтому их обязан блокировать слой query_only=ON на уровне движка. Главное
# here: данные не меняются + exit(1); текст ошибки SQLite варьируется
# (write-block для DELETE/UPDATE, схема-валидация для INSERT с неверным числом
# колонок — но запись НЕ происходит ни в одном случае).
_WITH_WRITE_STMTS = [
    "WITH c AS (SELECT 1) DELETE FROM actions",
    "WITH c AS (SELECT 1) UPDATE actions SET status = 'success'",
    "WITH c AS (SELECT 1) INSERT INTO actions SELECT 1 FROM c",
]


@pytest.mark.parametrize("sql", _DIRECT_WRITE_STMTS)
def test_query_rejects_direct_write_with_message(capsys, tmp_path, sql):
    """Прямая мутирующая инструкция → отказ с понятным сообщением, exit 1,
    данные не тронуты."""
    db = _seed_history(tmp_path)
    n_before = _count_actions(db)

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(db, sql=sql))
    assert exc.value.code == 1

    err = capsys.readouterr().err
    # понятное человеку сообщение о заприте записи
    assert "SELECT" in err or "select" in err.lower() or "read-only" in err.lower()

    # БД не изменилась — read-only гарантия
    assert _count_actions(db) == n_before


@pytest.mark.parametrize("sql", _WITH_WRITE_STMTS)
def test_query_blocks_with_wrapped_write(tmp_path, sql):
    """``WITH ... DELETE/UPDATE/INSERT`` начинается с WITH (guard пропускает) —
    блокируется слоем query_only=ON. Регрессия (Codex, критический): craft пути
    переопределял mode=ro, и эти формы мутировали боевую БД. Данные обязаны
    остаться нетронутыми, команда — exit(1)."""
    db = _seed_history(tmp_path)
    n_before = _count_actions(db)

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(db, sql=sql))
    assert exc.value.code == 1

    # КРИТИЧНО: ни одна строка не удалена/изменена/добавлена
    assert _count_actions(db) == n_before


def test_query_rejects_leading_whitespace_before_insert(tmp_path):
    """Пробелы/переносы перед INSERT не обходят guard."""
    db = _seed_history(tmp_path)

    with pytest.raises(SystemExit):
        query_cmd.run(_args(db, sql="   \n  DELETE FROM actions"))


def test_query_allows_with_select(capsys, tmp_path):
    """WITH ... SELECT — допустимый read-only запрос (CTE)."""
    db = _seed_history(tmp_path)

    query_cmd.run(
        _args(
            db,
            sql="WITH c AS (SELECT COUNT(*) AS n FROM actions) SELECT n FROM c",
        )
    )
    out = capsys.readouterr().out
    assert "2" in out


def test_query_allows_lowercase_select(capsys, tmp_path):
    """Регистр ключевого слова не важен: 'select ...' тоже read-only."""
    db = _seed_history(tmp_path)

    query_cmd.run(_args(db, sql="select count(*) as n from actions"))
    out = capsys.readouterr().out
    assert "2" in out


def test_query_rejects_sqlite_pragma_write(tmp_path):
    """PRAGMA без '=' (запись настроек) отсекается: мутирует состояние БД
    (например, PRAGMA journal_mode=... создаёт/меняет файл)."""
    db = _seed_history(tmp_path)
    with pytest.raises(SystemExit):
        query_cmd.run(_args(db, sql="PRAGMA journal_mode=WAL"))


def test_query_crafted_path_cannot_override_readonly(tmp_path):
    """Регрессия (Codex, критический): caller передаёт --history с суффиксом
    ``?mode=rw&...`` — без percent-encoding URI это переопределяло mode=ro, и
    ``WITH ... DELETE`` мутировал боевую БД. Третий слой (PRAGMA query_only=ON)
    обязан блокировать запись независимо от режима соединения/URI."""
    db = _seed_history(tmp_path)
    crafted = str(db) + "?mode=rw&ignored="  # попытка инъекции в URI
    n_before = _count_actions(db)

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(crafted, sql="WITH c AS (SELECT 1) DELETE FROM actions"))
    assert exc.value.code == 1

    # БД не тронута — критическая data-loss регрессия закрыта
    assert _count_actions(db) == n_before


def test_query_output_cannot_overwrite_history_db(tmp_path):
    """Регрессия (Codex, критический): ``query ... -o data/history.db``
    перезаписывал SQLite-файл ASCII-текстом → необратимая потеря истории.
    -o, резолвящийся в путь history (или alias того же inode), запрещён."""
    db = _seed_history(tmp_path)
    n_before = _count_actions(db)

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(db, sql="SELECT COUNT(*) FROM actions", output=db))
    assert exc.value.code == 1

    # БД осталась валидной SQLite — не перезаписана текстом
    assert _count_actions(db) == n_before
    # файл всё ещё SQLite (магические байты "SQLite format 3")
    assert db.read_bytes()[:15] == b"SQLite format 3"


def test_query_output_cannot_overwrite_history_db_via_symlink(tmp_path):
    """Тот же inode через symlink тоже запрещён (alias на history.db)."""
    db = _seed_history(tmp_path)
    alias = tmp_path / "history-link.db"
    alias.symlink_to(db)
    n_before = _count_actions(db)

    with pytest.raises(SystemExit):
        query_cmd.run(_args(db, sql="SELECT 1", output=alias))

    assert _count_actions(db) == n_before
    assert db.read_bytes()[:15] == b"SQLite format 3"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_query_output_cannot_overwrite_sqlite_companion(tmp_path, suffix):
    """Регрессия (Codex cycle-2, критический): ``-o data/history.db-wal``
    перезаписывал SQLite companion-файл. Во время конкурентной записи бота
    committed-транзакции могут жить в WAL/-journal до checkpoint; перезапись
    теряет данные или калечит БД. Companion-суффиксы главного пути запрещены."""
    db = _seed_history(tmp_path)
    companion = Path(str(db) + suffix)
    n_before = _count_actions(db)

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(db, sql="SELECT 1", output=companion))
    assert exc.value.code == 1

    # history.db не тронута (и companion не создан перезаписью)
    assert _count_actions(db) == n_before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_query_output_cannot_overwrite_companion_via_case_variant(tmp_path, suffix):
    """Регрессия (Codex cycle-3, критический): на case-insensitive FS
    ``history.db-WAL`` — тот же inode, что ``history.db-wal``, но resolve()
    сохраняет регистр caller'а → строковое сравнение суффикса пропускало.
    Case-вариант companion-пути тоже запрещён."""
    db = _seed_history(tmp_path)
    # uppercase-вариант суффикса (на APFS/HFS+ — alias того же файла)
    companion_upper = Path(str(db) + suffix.upper())
    n_before = _count_actions(db)

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(db, sql="SELECT 1", output=companion_upper))
    assert exc.value.code == 1
    assert _count_actions(db) == n_before


def test_query_output_cannot_overwrite_companion_via_hardlink(tmp_path):
    """Регрессия (Codex cycle-3, критический): hard link на существующий
    companion-файл имеет тот же inode, но другой путь — строковое сравнение
    его пропускало. samefile() по companion-кандидатам обязан его поймать.

    Тестируем инвариант на уровне _output_is_history (детерминированно, без
    открытия БД между шагами — оно меняет FS-состояние и рвёт hardlink):
    hardlink-alias на -wal должен распознаваться как «тот же inode, что
    companion-кандидат»."""
    db = tmp_path / "h.db"
    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"\x00" * 16)  # имитация живого WAL (без создания самой БД,
    # чтобы не открывать sqlite и не трогать FS-состояние до проверки)
    alias = tmp_path / "alias-wal"
    try:
        alias.hardlink_to(wal)  # Python 3.10+
    except (OSError, AttributeError):
        pytest.skip("hardlink unavailable on this FS/python")
    # sanity: alias реально тот же inode, что wal (иначе тест бессмысленен)
    if not alias.samefile(wal):
        pytest.skip("hardlink did not create same-inode alias on this FS")

    output_resolved = query_cmd._resolve_output_target(alias)
    # КЛЮЧЕВОЙ инвариант: hardlink на companion распознан как alias history
    assert query_cmd._output_is_history(output_resolved, db) is True


def test_query_output_nonexistent_dir_reports_error(capsys, tmp_path):
    """-o в несуществующий каталог → понятная ошибка + exit(1), а не
    необработанный FileNotFoundError с traceback (регрессия UX)."""
    db = _seed_history(tmp_path)
    bad_output = tmp_path / "no-such-dir" / "out.txt"

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(db, sql="SELECT 1", output=bad_output))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.strip() != ""
    assert "traceback" not in err.lower()


# --- -o <file> ---------------------------------------------------------------


def test_query_output_to_file(tmp_path, capsys):
    """-o <file>: результат пишется в файл, stdout пуст."""
    db = _seed_history(tmp_path)
    out_file = tmp_path / "report.txt"

    query_cmd.run(
        _args(db, sql="SELECT vacancy_id FROM actions ORDER BY vacancy_id", output=out_file)
    )

    captured = capsys.readouterr().out
    assert captured == ""  # в stdout ничего не ушло
    content = out_file.read_text(encoding="utf-8")
    assert "vacancy_id" in content
    assert "v1" in content and "v2" in content


def test_query_csv_output_to_file(tmp_path, capsys):
    """--csv -o <file>: CSV-файл."""
    db = _seed_history(tmp_path)
    out_file = tmp_path / "report.csv"

    query_cmd.run(
        _args(
            db, sql="SELECT vacancy_id FROM actions ORDER BY vacancy_id", csv=True, output=out_file
        )
    )

    assert capsys.readouterr().out == ""
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "vacancy_id"
    assert "v1" in lines and "v2" in lines


# --- ошибки SQL --------------------------------------------------------------


def test_query_invalid_sql_reports_error(capsys, tmp_path):
    """Синтаксически неверный SELECT → понятная ошибка, exit 1 (не падение)."""
    db = _seed_history(tmp_path)

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(db, sql="SELECT FROM actions WHERE"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.strip() != ""


# --- register / --help (характеризация argparse) -----------------------------


def _build_parser():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    query_cmd.register(sub)
    return p, sub


def test_query_registered_with_name():
    _, sub = _build_parser()
    assert "query" in sub.choices


def test_query_positional_sql():
    _, sub = _build_parser()
    opts = {a.option_strings[0] for a in sub.choices["query"]._actions if a.option_strings}
    assert "--csv" in opts
    assert "-o" in opts


def test_query_parses_csv_and_output():
    p, _ = _build_parser()
    args = p.parse_args(["query", "SELECT 1", "--csv", "-o", "out.csv"])
    assert args.sql == "SELECT 1"
    assert args.csv is True
    assert args.output == "out.csv"


# --- хелпер подсчёта строк (вне query, чтобы не зависеть от тестируемого) ----


def _count_actions(db_path) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM actions").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

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

import pytest

from hhru_bot.commands import query as query_cmd
from hhru_bot.history import History

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


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO actions (resume_id) VALUES ('x')",
        "UPDATE actions SET status = 'success'",
        "DELETE FROM actions",
        "DROP TABLE actions",
        "ALTER TABLE actions ADD COLUMN x TEXT",
        "CREATE TABLE evil (id INTEGER)",
    ],
)
def test_query_rejects_non_select(capsys, tmp_path, sql):
    """Любая мутирующая инструкция → отказ с понятным сообщением, exit 1,
    данные не тронуты."""
    db = _seed_history(tmp_path)
    n_before = _count_actions(db)

    with pytest.raises(SystemExit) as exc:
        query_cmd.run(_args(db, sql=sql))
    assert exc.value.code == 1

    err = capsys.readouterr().err
    # понятное человеку сообщение о запрете записи
    assert "SELECT" in err or "select" in err.lower() or "read-only" in err.lower()

    # БД не изменилась — read-only гарантия
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

"""Тесты команды ``account delete`` (только локальная файловая система)."""

from __future__ import annotations

import argparse
import sqlite3

import pytest

from hhru_bot.commands import account as account_cmd
from hhru_bot.write_lock import acquire_write_lock

pytestmark = pytest.mark.unit


def _args(name: str, *, dry_run: bool = False, force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(name=name, dry_run=dry_run, force=force)


def _make_account(tmp_path, name="marketing", *, with_history=True):
    account_dir = tmp_path / "data" / "accounts" / name
    account_dir.mkdir(parents=True)
    session = account_dir / "session.json"
    (account_dir / "config.yaml").write_text(
        "account:\n  storage_state_file: session.json\n", encoding="utf-8"
    )
    session.write_text("{}", encoding="utf-8")
    if with_history:
        conn = sqlite3.connect(account_dir / "history.db")
        conn.execute("CREATE TABLE actions (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("CREATE TABLE skipped (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO actions(status) VALUES (?)", [("success",), ("failed",)])
        conn.execute("INSERT INTO skipped(id) VALUES (1)")
        conn.commit()
        conn.close()
    return account_dir


def test_default_prints_plan_and_deletes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    account_dir = _make_account(tmp_path)

    assert account_cmd.run_delete(_args("marketing")) is False

    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out and "ничего не удалено" in out
    assert "accounts/marketing/config.yaml" in out
    assert "(записей: 3)" in out
    assert "accounts/marketing/session.json" in out
    assert account_dir.is_dir()


def test_explicit_dry_run_flag_prints_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    account_dir = _make_account(tmp_path)

    assert account_cmd.run_delete(_args("marketing", dry_run=True)) is False

    assert "[DRY-RUN]" in capsys.readouterr().out
    assert account_dir.is_dir()


def test_plan_reports_missing_history_and_session(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    account_dir = _make_account(tmp_path, with_history=False)
    (account_dir / "session.json").unlink()

    assert account_cmd.run_delete(_args("marketing")) is False

    out = capsys.readouterr().out
    assert "отсутствует или не читается" in out
    assert "(нет)" in out


def test_plan_survives_broken_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    account_dir = tmp_path / "data" / "accounts" / "broken"
    account_dir.mkdir(parents=True)
    (account_dir / "config.yaml").write_text("account: [", encoding="utf-8")

    assert account_cmd.run_delete(_args("broken")) is False

    out = capsys.readouterr().out
    assert "ошибка конфига" in out
    assert account_dir.is_dir()


def test_force_deletes_account_and_spares_neighbors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    account_dir = _make_account(tmp_path)
    neighbor = _make_account(tmp_path, name="other")

    assert account_cmd.run_delete(_args("marketing", force=True)) is False

    assert not account_dir.exists()
    assert neighbor.is_dir()
    assert '[OK] Аккаунт "marketing" удалён' in capsys.readouterr().out


def test_dry_run_wins_over_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    account_dir = _make_account(tmp_path)

    assert account_cmd.run_delete(_args("marketing", dry_run=True, force=True)) is False

    assert account_dir.is_dir()
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_unknown_account_fails_without_creating_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert account_cmd.run_delete(_args("ghost")) is True

    assert "[FAIL]" in capsys.readouterr().out
    assert not (tmp_path / "data" / "accounts" / "ghost").exists()


def test_rejects_path_traversal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert account_cmd.run_delete(_args("../outside")) is True

    assert "недопустимое имя" in capsys.readouterr().out
    assert (tmp_path / "outside").exists() is False


def test_plan_mode_skips_setup_logging_but_force_logs(tmp_path, monkeypatch):
    """Plan-only delete must not create data/logs as a side effect (READ, #21)."""
    _make_account(tmp_path)
    monkeypatch.chdir(tmp_path)
    from hhru_bot import cli

    called = []
    monkeypatch.setattr(cli, "setup_logging", lambda **kwargs: called.append(kwargs))

    cli._execute(cli.build_parser().parse_args(["account", "delete", "marketing"]))
    assert called == []

    # --force логируется как WRITE-мутация ещё до резолва имени (тут — [FAIL]).
    with pytest.raises(SystemExit):
        cli._execute(cli.build_parser().parse_args(["account", "delete", "ghost", "--force"]))
    assert len(called) == 1


def test_refuses_delete_while_write_lock_held(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    account_dir = _make_account(tmp_path)
    lock_path = account_dir / ".hhru.lock"

    with acquire_write_lock(lock_path, command="bump"):
        assert account_cmd.run_delete(_args("marketing", force=True)) is True

    out = capsys.readouterr().out
    assert "write-lock" in out and "идёт прогон" in out
    assert account_dir.is_dir()
    assert (account_dir / "config.yaml").is_file()

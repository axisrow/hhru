"""Тесты команды ``account create`` (только локальная файловая система)."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime

import pytest

from hhru_bot.commands import account as account_cmd

pytestmark = pytest.mark.unit


def _args(name: str) -> argparse.Namespace:
    return argparse.Namespace(name=name)


def test_create_copies_template(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    template = tmp_path / "config" / "config.example.yaml"
    template.parent.mkdir()
    template.write_text(
        "account:\n  storage_state_file: storage_state/session.json\n", encoding="utf-8"
    )

    assert account_cmd.run_create(_args("marketing")) is False
    created = tmp_path / "data" / "accounts" / "marketing" / "config.yaml"
    assert created.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")
    assert 'Аккаунт "marketing" создан' in capsys.readouterr().out
    if os.name != "nt":
        assert (created.parent.stat().st_mode & 0o777) == 0o700


def test_create_does_not_overwrite_existing_account(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    template = tmp_path / "config" / "config.example.yaml"
    template.parent.mkdir()
    template.write_text("new template", encoding="utf-8")
    account_dir = tmp_path / "data" / "accounts" / "marketing"
    account_dir.mkdir(parents=True)
    config = account_dir / "config.yaml"
    config.write_text("personal config", encoding="utf-8")

    assert account_cmd.run_create(_args("marketing")) is True
    assert config.read_text(encoding="utf-8") == "personal config"
    assert "перезапись запрещена" in capsys.readouterr().out


def test_create_rejects_path_traversal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    template = tmp_path / "config" / "config.example.yaml"
    template.parent.mkdir()
    template.write_text("template", encoding="utf-8")

    assert account_cmd.run_create(_args("../outside")) is True
    assert "недопустимое имя" in capsys.readouterr().out


def test_create_cleans_up_partial_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    template = tmp_path / "config" / "config.example.yaml"
    template.parent.mkdir()
    template.write_text("template", encoding="utf-8")

    def fail_after_partial_write(_source, destination):
        destination.write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(account_cmd.shutil, "copyfile", fail_after_partial_write)
    with pytest.raises(OSError, match="disk full"):
        account_cmd.create_account("marketing")
    assert not (tmp_path / "data" / "accounts" / "marketing").exists()


def test_scan_accounts_lists_configs_and_history_state(tmp_path):
    accounts_dir = tmp_path / "data" / "accounts"
    for name in ("zeta", "alpha"):
        (accounts_dir / name).mkdir(parents=True)
        (accounts_dir / name / "config.yaml").write_text("{}\n", encoding="utf-8")
    (accounts_dir / "alpha" / "history.db").touch()
    (accounts_dir / "ignored").mkdir()
    (accounts_dir / "ignored" / "notes.txt").touch()

    accounts = account_cmd.scan_accounts(tmp_path / "data")

    assert [(account.name, account.history_exists) for account in accounts] == [
        ("alpha", True),
        ("zeta", False),
    ]
    assert accounts[0].config_path == accounts_dir / "alpha" / "config.yaml"
    assert accounts[0].session_status.startswith("нет (ошибка конфига:")
    assert accounts[0].last_action == "—"
    assert accounts[1].session_status.startswith("нет (ошибка конфига:")
    assert accounts[1].last_action == "—"


def test_scan_accounts_empty_when_directory_is_missing(tmp_path):
    assert account_cmd.scan_accounts(tmp_path / "data") == []


def test_scan_accounts_handles_invalid_yaml_config(tmp_path):
    account_dir = tmp_path / "data" / "accounts" / "broken"
    account_dir.mkdir(parents=True)
    (account_dir / "config.yaml").write_text("account: [", encoding="utf-8")

    account = account_cmd.scan_accounts(tmp_path / "data")[0]

    assert account.session_status.startswith("нет (ошибка конфига:")
    assert account.last_action == "—"


def test_run_list_prints_info_for_empty_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    account_cmd.run_list(argparse.Namespace())

    assert capsys.readouterr().out == (
        "[INFO] Аккаунтов не найдено. Используйте hhru account create <name>.\n"
    )


def test_run_list_prints_ascii_table(tmp_path, monkeypatch, capsys):
    account_dir = tmp_path / "data" / "accounts" / "work"
    account_dir.mkdir(parents=True)
    (account_dir / "config.yaml").write_text(
        "account:\n  storage_state_file: session.json\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    account_cmd.run_list(argparse.Namespace())

    output = capsys.readouterr().out
    assert "| name" in output and "config_path" in output and "history_exists" in output
    assert "session" in output and "last_action" in output
    assert "| work" in output and "data/accounts/work/config.yaml" in output
    assert "нет" in output


def _account_config(tmp_path, name="work"):
    account_dir = tmp_path / "data" / "accounts" / name
    account_dir.mkdir(parents=True)
    session = account_dir / "session.json"
    config = account_dir / "config.yaml"
    config.write_text("account:\n  storage_state_file: session.json\n", encoding="utf-8")
    return account_dir, config, session


def test_scan_accounts_reports_local_session_age_and_last_action(tmp_path):
    account_dir, _, session = _account_config(tmp_path)
    session.write_text(
        json.dumps({"cookies": [{"name": "hhtoken", "value": "secret"}]}),
        encoding="utf-8",
    )
    history_path = account_dir / "history.db"
    conn = sqlite3.connect(history_path)
    conn.execute(
        "CREATE TABLE actions (id INTEGER PRIMARY KEY, action TEXT, status TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO actions(action, status, created_at) VALUES (?, ?, ?)",
        [("apply", "success", "2026-08-20T10:00:00"), ("bump", "failed", "2026-08-21T11:00:00")],
    )
    conn.commit()
    conn.close()
    os.utime(session, (session.stat().st_atime, datetime(2026, 8, 20, 10).timestamp()))

    account = account_cmd.scan_accounts(tmp_path / "data")[0]

    assert account.session_status.startswith("есть (локальный маркер; возраст ")
    assert "дн." in account.session_status
    assert account.last_action == "bump / failed / 2026-08-21T11:00:00"


def test_run_list_does_not_create_missing_history_or_logs(tmp_path, monkeypatch, capsys):
    account_dir, _, _ = _account_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    account_cmd.run_list(argparse.Namespace())

    assert not (account_dir / "history.db").exists()
    assert not (tmp_path / "data" / "logs").exists()
    assert "last_action" in capsys.readouterr().out

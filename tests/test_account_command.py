"""Тесты команды ``account create`` (только локальная файловая система)."""

from __future__ import annotations

import argparse

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


def test_scan_accounts_empty_when_directory_is_missing(tmp_path):
    assert account_cmd.scan_accounts(tmp_path / "data") == []


def test_run_list_prints_info_for_empty_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    account_cmd.run_list(argparse.Namespace())

    assert capsys.readouterr().out == (
        "[INFO] Аккаунтов не найдено. Используйте hhru account create <name>.\n"
    )


def test_run_list_prints_ascii_table(tmp_path, monkeypatch, capsys):
    account_dir = tmp_path / "data" / "accounts" / "work"
    account_dir.mkdir(parents=True)
    (account_dir / "config.yaml").touch()
    monkeypatch.chdir(tmp_path)

    account_cmd.run_list(argparse.Namespace())

    output = capsys.readouterr().out
    assert "| name" in output and "config_path" in output and "history_exists" in output
    assert "| work" in output and "data/accounts/work/config.yaml" in output
    assert "нет" in output

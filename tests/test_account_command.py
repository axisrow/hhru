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

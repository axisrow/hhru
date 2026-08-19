"""Тесты локальной CLI-команды settings."""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import settings
from hhru_bot.history import History

pytestmark = pytest.mark.unit


def _args(path, **values):
    base = {"history": str(path), "key": None, "value": None}
    base.update(values)
    return argparse.Namespace(**base)


def test_history_settings_are_upserted_and_listed(tmp_path):
    history = History(tmp_path / "history.db")
    history.set_setting("user.email", "a@example.test")
    history.set_setting("disable_version", "true")
    history.set_setting("user.email", "b@example.test")

    assert history.get_setting("user.email") == "b@example.test"
    assert history.list_settings() == [
        {"key": "disable_version", "value": "true"},
        {"key": "user.email", "value": "b@example.test"},
    ]


def test_run_get_set_and_list(tmp_path, capsys):
    path = tmp_path / "history.db"
    settings.run(_args(path, key="answer", value="42"))
    assert capsys.readouterr().out == "[OK]\n"

    settings.run(_args(path, key="answer"))
    assert capsys.readouterr().out == "42\n"

    settings.run(_args(path))
    output = capsys.readouterr().out
    assert "Тип" in output and "Ключ" in output and "Значение" in output
    assert "str" in output and "answer" in output and "42" in output


def test_run_lists_boolean_as_bool(tmp_path, capsys):
    settings.run(_args(tmp_path / "history.db", key="enabled", value="false"))
    capsys.readouterr()
    settings.run(_args(tmp_path / "history.db"))
    assert "bool" in capsys.readouterr().out

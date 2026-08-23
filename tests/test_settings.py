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


def test_run_get_missing_key_reports_info_not_silence(tmp_path, capsys):
    """cycle-review (/review, PR #394): get_setting() возвращает None для

    отсутствующего ключа, и до фикса run() ничего не печатал и завершался
    с exit-кодом 0 — опечатка в ключе выглядела неотличимо от штатного
    (но пустого) успеха. Остальные READ-команды проекта на "не найдено"
    печатают явный маркер (см. commands/profile.py::run_unset — "[INFO] ...
    не найдено"), settings должна следовать тому же контракту.
    """
    settings.run(_args(tmp_path / "history.db", key="missing"))
    assert capsys.readouterr().out == '[INFO] настройка "missing" не найдена\n'


def test_run_set_records_completed_command_run(tmp_path, capsys):
    path = tmp_path / "history.db"

    settings.run(_args(path, key="answer", value="42"))

    assert capsys.readouterr().out == "[OK]\n"
    row = History(path).command_runs()[-1]
    assert row["command"] == "settings"
    assert row["status"] == "completed"
    assert row["requested_limit"] == 1
    assert row["attempted"] == 1
    assert row["success"] == 1


def test_run_set_respects_command_run_lease(tmp_path, capsys):
    path = tmp_path / "history.db"
    history = History(path)
    history.start_command_run(command="copy-resume", requested_limit=None)

    result = settings.run(_args(path, key="answer", value="42"))

    assert result is True
    assert "supervised-команда уже выполняется" in capsys.readouterr().out
    assert history.get_setting("answer") is None

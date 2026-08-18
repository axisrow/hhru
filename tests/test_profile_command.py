"""Тесты локальной CLI-команды profile."""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import profile
from hhru_bot.history import History

pytestmark = pytest.mark.unit


def _args(path, **values):
    base = {"history": str(path)}
    base.update(values)
    return argparse.Namespace(**base)


def test_set_normalizes_label_and_writes_manual_value(tmp_path, capsys):
    path = tmp_path / "history.db"
    profile.run_set(_args(path, label="  Телефон\n", value="+7 900"))

    assert History(path).get_profile_answers() == {"телефон": "+7 900"}
    assert '[OK] Профиль обновлён: "  Телефон' in capsys.readouterr().out


def test_show_prints_ascii_table(capsys, tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_profile_field("Имя", "Анна", source="hh_ru")
    history.upsert_profile_field("Имя", "Alice", source="manual")

    profile.run_show(_args(tmp_path / "history.db"))
    output = capsys.readouterr().out
    assert "question_key" in output
    assert "source" in output
    assert "имя" in output
    assert "hh_ru" in output and "manual" in output


def test_unset_removes_only_manual_value(capsys, tmp_path):
    history = History(tmp_path / "history.db")
    history.upsert_profile_field("Имя", "Анна", source="hh_ru")
    history.upsert_profile_field("Имя", "Alice", source="manual")

    profile.run_unset(_args(tmp_path / "history.db", label=" ИМЯ "))

    assert History(tmp_path / "history.db").list_profile_fields() == [
        {
            "question_key": "имя",
            "value": "Анна",
            "source": "hh_ru",
            "updated_at": history.list_profile_fields()[0]["updated_at"],
        }
    ]
    assert "[OK]" in capsys.readouterr().out


def test_unset_missing_field_is_informational(capsys, tmp_path):
    profile.run_unset(_args(tmp_path / "history.db", label="Telegram"))
    assert "[INFO]" in capsys.readouterr().out

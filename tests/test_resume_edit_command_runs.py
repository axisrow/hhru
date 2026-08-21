"""Durable-ledger wiring for the single-mutation resume edit commands (#465)."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path

import pytest

from hhru_bot.history import History

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("module_name", "command_name"),
    [
        ("edit_experience", "edit_experience"),
        ("edit_education", "edit_education"),
        ("edit_skills", "edit_skills"),
        ("edit_languages", "edit_languages"),
        ("resume_position", "resume_position"),
    ],
)
def test_successful_resume_edit_persists_one_complete_command_run(
    tmp_path: Path, monkeypatch, capsys, module_name: str, command_name: str
) -> None:
    command = importlib.import_module(f"hhru_bot.commands.{module_name}")

    def mutation(_args, progress):
        progress.begin_attempt()
        progress.applied_count += 1
        return False

    monkeypatch.setattr(command, "_run", mutation)
    history_path = tmp_path / "history.db"

    assert command.run(argparse.Namespace(history=str(history_path))) is False

    row = History(history_path).command_runs()[-1]
    assert row["command"] == command_name
    assert row["status"] == "completed"
    assert row["attempted"] == row["success"] == 1
    assert row["failed"] == row["uncertain"] == row["skipped"] == 0
    assert "attempted=1 success=1 failed=0 uncertain=0 skipped=0" in capsys.readouterr().out

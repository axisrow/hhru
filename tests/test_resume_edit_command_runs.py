"""Durable-ledger wiring for the single-mutation resume edit commands (#465)."""

from __future__ import annotations

import argparse
import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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
def test_failed_mutation_after_attempt_persists_partial_status(
    tmp_path: Path, monkeypatch, module_name: str, command_name: str
) -> None:
    """A failure AFTER begin_attempt() must record status='partial' (#465 review).

    Regression guard for the edit_languages.py bug found in cycle-review of
    PR #472: a body that still called sys.exit()/raised SystemExit past the
    attempt-reservation point escaped run_supervised_command's normal
    bool-based classification (the generic ``except BaseException`` branch
    never computes final_status), recording status='failed' instead of the
    'partial' every other command produces for the identical one-attempt-
    failed outcome.
    """
    command = importlib.import_module(f"hhru_bot.commands.{module_name}")

    def mutation(_args, progress):
        progress.begin_attempt()
        progress.failed_count += 1
        return True

    monkeypatch.setattr(command, "_run", mutation)
    history_path = tmp_path / "history.db"

    assert command.run(argparse.Namespace(history=str(history_path))) is True

    row = History(history_path).command_runs()[-1]
    assert row["command"] == command_name
    assert row["status"] == "partial"
    assert row["attempted"] == row["failed"] == 1
    assert row["success"] == row["uncertain"] == row["skipped"] == 0


def test_edit_languages_manual_write_failure_records_partial_not_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for the dead-code/status bug found in cycle-review of
    PR #472: a failed manual ``--language`` write must return True (not raise
    SystemExit via a leftover ``_report()`` call) and the ledger must show
    ``status='partial'`` like every sibling command, not ``'failed'``.
    """
    import hhru_bot.commands.edit_languages as command
    from hhru_bot.languages import Language, LanguagesResult

    resume = SimpleNamespace(id="r1", resume_id="r1")
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.languages.edit_languages_on_hh",
        lambda *_a, **_kw: LanguagesResult(
            success=False, proposed=(Language("English", "B1"),), reason="запись не подтверждена"
        ),
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        mode="append",
        language=["English=B1"],
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "запись не подтверждена" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_languages"
    assert row["status"] == "partial"
    assert row["attempted"] == row["failed"] == 1
    assert row["success"] == row["uncertain"] == row["skipped"] == 0


@pytest.mark.parametrize(
    ("module_name", "command_name"),
    [
        ("edit_education", "edit_education"),
        ("edit_experience", "edit_experience"),
    ],
)
def test_uncertain_outcome_is_not_counted_as_failed(
    tmp_path: Path, monkeypatch, module_name: str, command_name: str
) -> None:
    """An 'uncertain' mutation outcome must land in progress.uncertain_count,
    not failed_count (#465 review): CLAUDE.md/#176 treat 'uncertain' as
    'may have landed', which a ledger reading failed=1 uncertain=0 hides from
    the operator exactly the way this project fails closed against.
    """
    command = importlib.import_module(f"hhru_bot.commands.{module_name}")

    def mutation(_args, progress):
        progress.begin_attempt()
        progress.uncertain_count += 1
        return True

    monkeypatch.setattr(command, "_run", mutation)
    history_path = tmp_path / "history.db"

    assert command.run(argparse.Namespace(history=str(history_path))) is True

    row = History(history_path).command_runs()[-1]
    assert row["command"] == command_name
    assert row["attempted"] == row["uncertain"] == 1
    assert row["failed"] == row["success"] == row["skipped"] == 0

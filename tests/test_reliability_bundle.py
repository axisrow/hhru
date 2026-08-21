from __future__ import annotations

import argparse
import json
import signal
import sqlite3
from pathlib import Path

import pytest

from hhru_bot import cli
from hhru_bot.apply.pipeline import ApplyContext
from hhru_bot.exit_codes import CommandExitCode
from hhru_bot.history import History
from hhru_bot.responses import ResponseItem
from hhru_bot.search import VacancyCard
from hhru_bot.write_lock import WriteLockBusy, acquire_write_lock

pytestmark = pytest.mark.integration


def _card(vacancy_id: str = "123") -> VacancyCard:
    return VacancyCard(vacancy_id, "Python", "ACME", f"https://hh.ru/vacancy/{vacancy_id}")


def test_old_database_gets_apply_run_and_action_correlation_columns(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resume_id TEXT NOT NULL,
                vacancy_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                search_query TEXT,
                created_at TEXT NOT NULL
            )"""
        )

    history = History(db)
    with history._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(actions)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}

    assert {"run_id", "reason_code"} <= columns
    assert "apply_runs" in tables


def test_apply_run_recovers_orphan_and_persists_counters(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")
    first = history.start_apply_run(command="apply", requested_limit=3)
    second = history.start_apply_run(command="apply", requested_limit=2)
    history.finish_apply_run(
        second,
        status="completed",
        exit_code=0,
        attempted=4,
        success=2,
        failed=1,
        uncertain=1,
        skipped=0,
    )

    rows = {row["run_id"]: row for row in history.apply_runs()}
    assert rows[first]["status"] == "orphaned"
    assert rows[first]["finished_at"]
    assert rows[second]["success"] == 2
    assert rows[second]["uncertain"] == 1

    action_id = history.record_action(
        "resume",
        "987",
        "apply",
        "success",
        run_id=second,
        reason_code="reconciled_success",
    )
    with history._connect() as conn:
        action = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert action["run_id"] == second
    assert action["reason_code"] == "reconciled_success"


def test_review_requeue_only_failed_and_clears_permit(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")
    item_id = history.enqueue_review("resume", _card(), 1.0, {}, "letter")
    history.approve_review(item_id)
    history.finish_review(item_id, "failed")

    history.requeue_review(item_id)
    row = next(row for row in history.review_items() if row["id"] == item_id)
    assert row["status"] == "pending"
    assert row["permit_hash"] is None
    assert row["permit_expires_at"] is None

    with pytest.raises(ValueError, match="только failed"):
        history.requeue_review(item_id)


@pytest.mark.parametrize("status", ["success", "uncertain"])
def test_review_requeue_rejects_external_success_or_uncertain(tmp_path: Path, status: str) -> None:
    history = History(tmp_path / "history.db")
    item_id = history.enqueue_review("resume", _card(), 1.0, {}, "letter")
    history.finish_review(item_id, "failed")
    history.record_action("resume", "123", "apply", status)

    with pytest.raises(ValueError, match="безопасный повтор запрещён"):
        history.requeue_review(item_id)


def test_outcome_codes_are_machine_readable() -> None:
    ctx = ApplyContext(object(), _card(), "resume", "hello", False)
    assert ctx.ok("done").outcome_code == "success"
    assert ctx.fail("bad").outcome_code == "failed"
    assert ctx.skip("questions").outcome_code == "skipped"


def test_exit_codes_cover_persistence_and_sigterm() -> None:
    assert CommandExitCode.PERSISTENCE_FAILED.value == 2
    assert CommandExitCode.SIGTERM.value == 143


def test_lock_file_contains_owner_metadata(tmp_path: Path) -> None:
    lock = tmp_path / ".hhru.lock"
    with acquire_write_lock(lock, command="probe --questionnaires-only"):
        owner = json.loads(lock.read_text())
        assert owner["pid"] > 0
        assert owner["command"] == "probe --questionnaires-only"
        assert owner["started_at"]

        with pytest.raises(WriteLockBusy) as error:
            with acquire_write_lock(lock, command="apply"):
                pass
        assert error.value.owner["pid"] == owner["pid"]


def test_response_item_carries_ssr_resume_id() -> None:
    item = ResponseItem("123", "response", topic="topic-1", resume_id="resume-9")
    assert item.resume_id == "resume-9"


def test_questionnaire_probe_is_a_local_write_command() -> None:
    args = cli.build_parser().parse_args(["probe", "--questionnaires-only"])
    assert cli._is_write_command(args)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(KeyboardInterrupt(), CommandExitCode.SIGINT), (signal.SIGTERM, CommandExitCode.SIGTERM)],
)
def test_apply_run_persists_typed_signal_exit(
    tmp_path: Path, monkeypatch, failure, expected: CommandExitCode
) -> None:
    from hhru_bot.commands import apply as apply_command

    config = object()
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)

    def interrupt(_args, _config, _history, progress):
        progress.begin_attempt()
        if failure == signal.SIGTERM:
            signal.raise_signal(signal.SIGTERM)
        raise failure

    monkeypatch.setattr(apply_command, "_run", interrupt)
    args = argparse.Namespace(
        config="unused",
        history=str(tmp_path / "history.db"),
        command="apply",
        limit=1,
        approved=None,
        dry_run=False,
    )

    assert apply_command.run(args) is expected
    row = History(args.history).apply_runs()[-1]
    assert row["status"] == "interrupted"
    assert row["exit_code"] == expected.value
    assert row["attempted"] == 1
    assert row["failed"] == 1  # interruption happened before durable submit reservation


def test_combined_run_does_not_bump_after_typed_interrupt(monkeypatch) -> None:
    from hhru_bot.commands import run as run_command

    monkeypatch.setattr(run_command.apply_cmd, "run", lambda _args: CommandExitCode.SIGINT)
    monkeypatch.setattr(
        run_command.bump_cmd,
        "run",
        lambda _args: pytest.fail("bump must not run after interrupted apply"),
    )

    assert run_command.run(argparse.Namespace()) is CommandExitCode.SIGINT

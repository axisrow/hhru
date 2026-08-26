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


def test_old_database_gets_command_run_and_action_correlation_columns(tmp_path: Path) -> None:
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
    assert "command_runs" in tables


def test_apply_runs_table_migrates_to_command_runs(tmp_path: Path) -> None:
    # #461: старая БД с apply_runs (PR #460) должна получить переименованную
    # таблицу command_runs с сохранёнными данными и переименованным индексом,
    # а не вторую параллельную таблицу.
    db = tmp_path / "history.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE apply_runs (
                run_id TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                requested_limit INTEGER,
                status TEXT NOT NULL,
                attempted INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                uncertain INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                exit_code INTEGER,
                detail TEXT
            )"""
        )
        conn.execute("CREATE INDEX idx_apply_runs_status ON apply_runs(status, started_at)")
        conn.execute(
            """INSERT INTO apply_runs (run_id, command, status, started_at)
               VALUES ('old-run', 'apply', 'completed', '2026-01-01T00:00:00')"""
        )

    history = History(db)
    with history._connect() as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }

    assert "command_runs" in tables
    assert "apply_runs" not in tables
    assert "idx_command_runs_status" in indexes
    assert "idx_apply_runs_status" not in indexes

    rows = {row["run_id"]: row for row in history.command_runs()}
    assert rows["old-run"]["status"] == "completed"

    # Идемпотентность: повторное открытие той же (уже смигрированной) БД не
    # должно падать и не должно создавать вторую таблицу.
    History(db)
    with sqlite3.connect(db) as conn:
        tables_again = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "apply_runs" not in tables_again
    assert "command_runs" in tables_again


def test_command_run_recovers_orphan_and_persists_counters(tmp_path: Path, monkeypatch) -> None:
    history = History(tmp_path / "history.db")
    first = history.start_command_run(command="apply", requested_limit=3)
    monkeypatch.setattr("hhru_bot.history._pid_is_alive", lambda _pid: False)
    second = history.start_command_run(command="apply", requested_limit=2)
    history.finish_command_run(
        second,
        status="completed",
        exit_code=0,
        attempted=4,
        success=2,
        failed=1,
        uncertain=1,
        skipped=0,
    )

    rows = {row["run_id"]: row for row in history.command_runs()}
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


def test_finalize_action_without_reason_code_keeps_started_marker(tmp_path: Path) -> None:
    # cycle-review PR #460 (round 3, Claude /review): finalize_action wrote
    # reason_code unconditionally (default None), so a caller omitting the
    # kwarg (several do -- the AntiBotChallengeDetected and skip paths in
    # commands/_common.py, and clear_negotiations.py) silently overwrote
    # begin_action's "started" audit marker with NULL. COALESCE keeps the
    # existing value when the caller passes None.
    history = History(tmp_path / "history.db")
    action_id = history.begin_action("resume", "123", "apply")

    with history._connect() as conn:
        before = conn.execute("SELECT reason_code FROM actions WHERE id=?", (action_id,)).fetchone()
    assert before["reason_code"] == "started"

    history.finalize_action(action_id, "failed", "кнопка не найдена")  # no reason_code kwarg

    with history._connect() as conn:
        after = conn.execute("SELECT reason_code FROM actions WHERE id=?", (action_id,)).fetchone()
    assert after["reason_code"] == "started"  # not silently nulled

    history.finalize_action(action_id, "success", "done", reason_code="reconciled_success")
    with history._connect() as conn:
        overwritten = conn.execute(
            "SELECT reason_code FROM actions WHERE id=?", (action_id,)
        ).fetchone()
    assert overwritten["reason_code"] == "reconciled_success"  # explicit value still wins


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
        owner = json.loads(lock.with_name(f"{lock.name}.owner").read_text())
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
def test_command_run_persists_typed_signal_exit(
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
    row = History(args.history).command_runs()[-1]
    assert row["status"] == "interrupted"
    assert row["exit_code"] == expected.value
    assert row["attempted"] == 1
    assert row["failed"] == 1  # interruption happened before durable submit reservation


def test_command_run_ledger_failure_does_not_mask_the_original_exception(
    tmp_path: Path, monkeypatch
) -> None:
    # cycle-review PR #460 (round 3, Claude /review): the `finally` block's
    # `history.finish_command_run(...)` call can itself raise ValueError (e.g.
    # if the run row is no longer 'running') while a real exception from
    # `_run` is already propagating through `except BaseException: ...;
    # raise` -- an exception raised inside `finally` replaces/masks the one
    # currently propagating (standard Python semantics), silently swallowing
    # the original crash. The ledger bookkeeping in `finally` must not be
    # allowed to eclipse a genuine in-flight exception.
    from hhru_bot.commands import apply as apply_command

    config = object()
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)

    def crash(_args, _config, history, progress):
        progress.begin_attempt()
        # Force finish_command_run to fail inside `finally`: mark the run
        # already finished before _run's own exception even starts
        # propagating, simulating a lost race / already-finalized run.
        history.finish_command_run(
            progress.run_id,
            status="completed",
            exit_code=0,
            attempted=0,
            success=0,
            failed=0,
            uncertain=0,
            skipped=0,
        )
        raise RuntimeError("boom: real pipeline crash")

    monkeypatch.setattr(apply_command, "_run", crash)
    args = argparse.Namespace(
        config="unused",
        history=str(tmp_path / "history.db"),
        command="apply",
        limit=1,
        approved=None,
        dry_run=False,
    )

    with pytest.raises(RuntimeError, match="boom: real pipeline crash"):
        apply_command.run(args)


def test_combined_run_does_not_bump_after_typed_interrupt(monkeypatch) -> None:
    from hhru_bot.commands import run as run_command

    monkeypatch.setattr(run_command.apply_cmd, "run", lambda _args: CommandExitCode.SIGINT)
    monkeypatch.setattr(
        run_command.bump_cmd,
        "run",
        lambda _args: pytest.fail("bump must not run after interrupted apply"),
    )

    assert run_command.run(argparse.Namespace()) is CommandExitCode.SIGINT

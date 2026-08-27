"""``commands/_common.run_supervised_command`` in isolation (#462).

Extracted from ``commands/apply.py`` (#460/#461 had it apply-specific) so any
future WRITE-hh.ru command can reuse the same SIGINT/SIGTERM handling and
machine-readable ``[RUN]`` summary. ``apply.py``'s own behaviour/tests are
covered separately in ``test_reliability_bundle.py``/``test_apply_limit.py``
and must stay green unchanged -- this file exercises the helper without any
apply-specific plumbing.
"""

from __future__ import annotations

import signal
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hhru_bot.browser import NotAuthenticated
from hhru_bot.commands._common import ApplyProgress, SignalTermination, run_supervised_command
from hhru_bot.exit_codes import CommandExitCode
from hhru_bot.history import LEGACY_LEASE_GRACE, CommandRunBusy, History

pytestmark = pytest.mark.integration


def test_completed_run_persists_summary_and_returns_false(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")

    def body(progress: ApplyProgress) -> bool:
        progress.begin_attempt()
        progress.applied_count += 1
        return False

    result = run_supervised_command(command="apply", history=history, requested_limit=1, body=body)

    assert result is False
    row = history.command_runs()[-1]
    assert row["status"] == "completed"
    assert row["exit_code"] == 0
    assert row["attempted"] == 1
    assert row["success"] == 1


def test_failed_run_without_attempts_is_failed_not_partial(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")

    result = run_supervised_command(
        command="apply", history=history, requested_limit=None, body=lambda _progress: True
    )

    assert result is True
    row = history.command_runs()[-1]
    assert row["status"] == "failed"
    assert row["exit_code"] == 1


def test_failed_run_with_attempts_is_partial(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")

    def body(progress: ApplyProgress) -> bool:
        progress.begin_attempt()
        return True

    result = run_supervised_command(
        command="apply", history=history, requested_limit=None, body=body
    )

    assert result is True
    row = history.command_runs()[-1]
    assert row["status"] == "partial"


def test_live_supervised_owner_rejects_competing_command_without_orphaning(
    tmp_path: Path,
) -> None:
    history = History(tmp_path / "history.db")
    active = history.start_command_run(command="apply", requested_limit=1)

    with pytest.raises(CommandRunBusy):
        history.start_command_run(command="clear-negotiations", requested_limit=None)

    rows = history.command_runs()
    assert len(rows) == 1
    assert rows[0]["run_id"] == active
    assert rows[0]["status"] == "running"


def _insert_running_row(history: History, *, owner_pid: int | None, started_at: datetime) -> str:
    """Insert a ``running`` command_runs row directly, bypassing the lease.

    Simulates a row written by an older binary predating the ``owner_pid``
    column (#479): ``_ensure_column``'s ``ALTER TABLE`` backfills existing
    rows with NULL, it never retrofits a live PID onto them.
    """
    run_id = str(uuid.uuid4())
    with history._connect() as conn:
        conn.execute(
            """INSERT INTO command_runs
               (run_id, command, requested_limit, status, started_at, owner_pid)
               VALUES (?, 'apply', NULL, 'running', ?, ?)""",
            (run_id, started_at.isoformat(), owner_pid),
        )
    return run_id


def test_recent_null_owner_row_blocks_competing_command_without_orphaning(
    tmp_path: Path,
) -> None:
    """A NULL-owner row younger than the grace window is treated as live (#479).

    Covers the migration-window race: an older binary, predating the
    ``owner_pid`` column, is still executing a supervised command when a
    freshly reinstalled binary starts and reads the same database. Without
    the grace window, the old binary's live row (owner_pid=NULL, just
    started) would be reclaimed unconditionally and a second supervised
    command would run concurrently against the same account.
    """
    history = History(tmp_path / "history.db")
    stale_run_id = _insert_running_row(history, owner_pid=None, started_at=datetime.now())

    with pytest.raises(CommandRunBusy):
        history.start_command_run(command="clear-negotiations", requested_limit=None)

    rows = history.command_runs()
    assert len(rows) == 1
    assert rows[0]["run_id"] == stale_run_id
    assert rows[0]["status"] == "running"


def test_old_null_owner_row_is_still_reclaimed(tmp_path: Path) -> None:
    """A NULL-owner row older than the grace window is reclaimed as before.

    Preserves pre-#479 behaviour for genuinely stale legacy rows: an
    unbounded lease was never the intent, only closing the narrow
    rolling-upgrade overlap.
    """
    history = History(tmp_path / "history.db")
    old_started_at = datetime.now() - LEGACY_LEASE_GRACE - timedelta(minutes=1)
    stale_run_id = _insert_running_row(history, owner_pid=None, started_at=old_started_at)

    new_run_id = history.start_command_run(command="apply", requested_limit=1)

    rows = {row["run_id"]: row for row in history.command_runs()}
    assert rows[stale_run_id]["status"] == "orphaned"
    assert rows[new_run_id]["status"] == "running"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(KeyboardInterrupt(), CommandExitCode.SIGINT), (signal.SIGTERM, CommandExitCode.SIGTERM)],
)
def test_typed_signal_exit_is_persisted(tmp_path: Path, failure, expected: CommandExitCode) -> None:
    history = History(tmp_path / "history.db")

    def body(progress: ApplyProgress) -> bool:
        progress.begin_attempt()
        if failure == signal.SIGTERM:
            signal.raise_signal(signal.SIGTERM)
        raise failure

    result = run_supervised_command(
        command="apply", history=history, requested_limit=None, body=body
    )

    assert result is expected
    row = history.command_runs()[-1]
    assert row["status"] == "interrupted"
    assert row["exit_code"] == expected.value


def test_sigterm_handler_is_restored_after_normal_return(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")
    previous = signal.getsignal(signal.SIGTERM)

    run_supervised_command(
        command="apply", history=history, requested_limit=None, body=lambda _progress: False
    )

    assert signal.getsignal(signal.SIGTERM) is previous


def test_sigterm_handler_is_restored_after_exception(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")
    previous = signal.getsignal(signal.SIGTERM)

    def body(_progress: ApplyProgress) -> bool:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_supervised_command(command="apply", history=history, requested_limit=None, body=body)

    assert signal.getsignal(signal.SIGTERM) is previous


def test_generic_exception_persists_failed_status_with_detail(tmp_path: Path) -> None:
    # cycle-review PR #468 (code-reviewer-462): the `except BaseException as
    # exc: detail = ...; raise` path had no direct test -- only its handler-
    # restoration side effect and the (deliberately finish_command_run-
    # breaking) masking-guard test covered it, and the latter never lets
    # `detail` reach the ledger row. Assert the row directly.
    history = History(tmp_path / "history.db")

    def body(_progress: ApplyProgress) -> bool:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_supervised_command(command="apply", history=history, requested_limit=None, body=body)

    row = history.command_runs()[-1]
    assert row["status"] == "failed"
    assert row["exit_code"] == 1
    assert row["detail"] == "RuntimeError: boom"


def test_expired_session_has_dedicated_exit_code_and_no_uncertain_count(
    tmp_path: Path, capsys
) -> None:
    """A pre-action auth failure stops the batch and remains outside grey-zone accounting."""
    history = History(tmp_path / "history.db")
    seen: list[int] = []

    def body(_progress: ApplyProgress) -> bool:
        seen.append(1)
        raise NotAuthenticated("cookie hhtoken не найден")

    result = run_supervised_command(command="apply", history=history, requested_limit=5, body=body)

    assert result is CommandExitCode.SESSION_EXPIRED
    assert seen == [1]
    row = history.command_runs()[-1]
    assert row["status"] == "failed"
    assert row["exit_code"] == CommandExitCode.SESSION_EXPIRED.value
    assert row["attempted"] == 0
    assert row["uncertain"] == 0
    output = capsys.readouterr().out
    assert "hhru login" in output
    assert "hhru refresh-token" in output


def test_nested_call_restores_outer_handler_lifo(tmp_path: Path) -> None:
    # #462 review risk called out in the issue body: re-entrancy. A nested
    # call's own SIGTERM handler must not clobber the outer call's handler
    # on the way out -- restoration is LIFO via the previous handler each
    # call captured before installing its own.
    history = History(tmp_path / "history.db")
    previous = signal.getsignal(signal.SIGTERM)
    outer_handler_during_inner = None

    def inner_body(_progress: ApplyProgress) -> bool:
        return False

    def outer_body(_progress: ApplyProgress) -> bool:
        nonlocal outer_handler_during_inner
        run_supervised_command(
            command="apply", history=history, requested_limit=None, body=inner_body
        )
        outer_handler_during_inner = signal.getsignal(signal.SIGTERM)
        return False

    run_supervised_command(command="apply", history=history, requested_limit=None, body=outer_body)

    # After the inner call returns, the outer call's own handler (installed
    # before outer_body ran) must still be in place, not the pre-outer one.
    assert outer_handler_during_inner is not previous
    assert signal.getsignal(signal.SIGTERM) is previous


def test_ledger_failure_does_not_mask_the_original_exception(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")

    def body(progress: ApplyProgress) -> bool:
        # Force finish_command_run to fail inside the helper's `finally`:
        # mark the run already finished before body's own exception starts
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

    with pytest.raises(RuntimeError, match="boom: real pipeline crash"):
        run_supervised_command(command="apply", history=history, requested_limit=None, body=body)


def test_reconcile_hook_runs_before_finish_and_summary(tmp_path: Path) -> None:
    history = History(tmp_path / "history.db")
    seen_run_id = None

    def body(progress: ApplyProgress) -> bool:
        progress.begin_attempt()
        return False

    def reconcile(progress: ApplyProgress, hist: History, run_id: str) -> None:
        nonlocal seen_run_id
        seen_run_id = run_id
        assert hist is history
        progress.applied_count = 42

    run_supervised_command(
        command="apply",
        history=history,
        requested_limit=None,
        body=body,
        reconcile=reconcile,
    )

    row = history.command_runs()[-1]
    assert seen_run_id == row["run_id"]
    assert row["success"] == 42


def test_signal_termination_is_a_base_exception() -> None:
    # Must stay BaseException (not Exception): pipeline code has broad
    # `except Exception` layers that must not silently swallow a SIGTERM
    # raised from inside the helper's handler.
    assert issubclass(SignalTermination, BaseException)
    assert not issubclass(SignalTermination, Exception)

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
from pathlib import Path

import pytest

from hhru_bot.commands._common import ApplyProgress, SignalTermination, run_supervised_command
from hhru_bot.exit_codes import CommandExitCode
from hhru_bot.history import History

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

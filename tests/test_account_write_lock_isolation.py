"""Multi-account behaviour of both write-serialization layers (#722).

``_write_lock_path`` (``cli.py``) resolves the non-blocking ``FileLock`` into
the *history.db*'s parent directory, and ``History.start_command_run``
(``history.py``) acquires a durable SQLite lease scoped to whichever
``history.db`` the caller opened. Neither is written per-account explicitly --
isolation is a side effect of each named account getting its own
``data/accounts/<name>/history.db``. This file pins that behaviour down for
both layers instead of leaving it merely implied by the code:

1. Two different accounts do NOT block each other (separate lock/lease
   scopes).
2. The same account blocks its own second concurrent run, on both layers.

Locks are intentionally non-blocking (``FileLock(timeout=0)``): a second
acquire must fail immediately, not wait. No sleep-based timing is used, so
this stays well inside the pytest-xdist suite budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hhru_bot.accounts import resolve_account_paths
from hhru_bot.cli import _write_lock_path, build_parser
from hhru_bot.history import CommandRunBusy, History
from hhru_bot.write_lock import WriteLockBusy, acquire_write_lock

pytestmark = pytest.mark.integration


def _write_account(data_dir: Path, name: str) -> Path:
    account_dir = data_dir / "accounts" / name
    account_dir.mkdir(parents=True)
    (account_dir / "config.yaml").write_text(
        "resumes: []\n",
        encoding="utf-8",
    )
    return resolve_account_paths(name, data_dir=data_dir).history


def _lock_path_for_account(tmp_path: Path, account: str) -> Path:
    data_dir = tmp_path / "data"
    history = _write_account(data_dir, account)
    args = build_parser().parse_args(["--account", account, "bump"])
    args.history = str(history)
    args.config = str(resolve_account_paths(account, data_dir=data_dir).config)
    return _write_lock_path(args)


def test_different_accounts_resolve_different_lock_paths(tmp_path):
    lock_a = _lock_path_for_account(tmp_path, "alpha")
    lock_b = _lock_path_for_account(tmp_path, "beta")

    assert lock_a != lock_b
    assert lock_a.parent != lock_b.parent


def test_different_accounts_hold_the_file_lock_simultaneously(tmp_path):
    lock_a = _lock_path_for_account(tmp_path, "alpha")
    lock_b = _lock_path_for_account(tmp_path, "beta")

    with acquire_write_lock(lock_a, command="bump"):
        # A concurrent write on a different account must not be rejected --
        # the two locks live in separate account directories.
        with acquire_write_lock(lock_b, command="bump"):
            pass


def test_same_account_second_file_lock_is_rejected(tmp_path):
    lock_a = _lock_path_for_account(tmp_path, "alpha")

    with acquire_write_lock(lock_a, command="bump"):
        with pytest.raises(WriteLockBusy):
            with acquire_write_lock(lock_a, command="apply"):
                pass


def test_different_accounts_acquire_independent_command_run_leases(tmp_path):
    data_dir = tmp_path / "data"
    history_a = History(_write_account(data_dir, "alpha"))
    history_b = History(_write_account(data_dir, "beta"))

    run_a = history_a.start_command_run(command="apply", requested_limit=1)
    # Beta's lease lives in a separate history.db; alpha holding its lease
    # must not block beta's own run.
    run_b = history_b.start_command_run(command="apply", requested_limit=1)

    assert run_a != run_b
    assert history_a.command_runs()[-1]["status"] == "running"
    assert history_b.command_runs()[-1]["status"] == "running"


def test_same_account_second_command_run_lease_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    history = History(_write_account(data_dir, "alpha"))

    active_run = history.start_command_run(command="apply", requested_limit=1)

    with pytest.raises(CommandRunBusy):
        history.start_command_run(command="bump", requested_limit=None)

    rows = history.command_runs()
    assert len(rows) == 1
    assert rows[0]["run_id"] == active_run
    assert rows[0]["status"] == "running"

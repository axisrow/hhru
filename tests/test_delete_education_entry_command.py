"""Контракт delete-education-entry: подтверждение, аудит, ретрай (#802)."""

from __future__ import annotations

import argparse
import signal
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.delete_education_entry as cmd
import hhru_bot.resume_education
from hhru_bot.history import History
from hhru_bot.resume_education import EducationDeleteResult

pytestmark = pytest.mark.integration

ENTRY_ID = "8798959"


def _config(tmp_path):
    return SimpleNamespace(storage_state_file=tmp_path / "session.json", user_agent=None)


def _args(tmp_path, **overrides):
    values = dict(
        config="unused",
        history=str(tmp_path / "history.db"),
        headless=True,
        entry_id=ENTRY_ID,
        kind="primary",
        dry_run=False,
        force=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = SimpleNamespace(result=EducationDeleteResult(ENTRY_ID, "primary", True, "удалено"))
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _: _config(tmp_path))

    @contextmanager
    def launch(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", launch)

    def delete(page, kind, entry_id, dry, *, before_click=None):  # noqa: ANN001, ARG001
        if not dry and (state.result.success or state.result.uncertain):
            if before_click is not None:
                before_click()
        return state.result

    monkeypatch.setattr(hhru_bot.resume_education, "delete_education_entry_on_hh", delete)
    return state


def test_dry_run_writes_nothing_to_history(env, tmp_path, capsys):
    env.result = EducationDeleteResult(
        ENTRY_ID, "primary", True, "dry-run; кнопка удаления не нажата"
    )
    cmd.run(_args(tmp_path, dry_run=True))
    assert "[DRY-RUN]" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT action, status FROM actions WHERE resume_id = ?", (ENTRY_ID,)
        ).fetchone()
    assert row is None


def test_no_flags_is_dry_run(env, tmp_path, capsys):
    env.result = EducationDeleteResult(
        ENTRY_ID, "primary", True, "dry-run; кнопка удаления не нажата"
    )
    cmd.run(_args(tmp_path, dry_run=False, force=False))
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_uncertain_is_audited_and_fails(env, tmp_path, capsys):
    env.result = EducationDeleteResult(
        ENTRY_ID, "primary", False, "ошибка после клика", uncertain=True
    )
    assert cmd.run(_args(tmp_path, force=True)) is True
    assert "uncertain" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute("SELECT status FROM actions WHERE resume_id = ?", (ENTRY_ID,)).fetchone()
    assert row["status"] == "uncertain"


def test_live_success_is_recorded_as_single_completed_run(env, tmp_path):
    assert cmd.run(_args(tmp_path, force=True)) is False
    run = History(tmp_path / "history.db").command_runs()[-1]
    assert (run["command"], run["status"], run["attempted"], run["success"], run["failed"]) == (
        "delete-education-entry",
        "completed",
        1,
        1,
        0,
    )


def test_not_found_result_is_reported_as_ok_success(env, tmp_path, capsys):
    """A retry that finds the entry already gone must complete, not fail (#802/#480)."""
    env.result = EducationDeleteResult(
        ENTRY_ID, "primary", True, "запись не найдена; уже отсутствует", not_found=True
    )
    assert cmd.run(_args(tmp_path, force=True)) is False
    assert "[OK]" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute("SELECT status FROM actions WHERE resume_id = ?", (ENTRY_ID,)).fetchone()
    assert row["status"] == "success"


def test_ambiguous_button_pre_click_failure_writes_nothing_to_history(env, tmp_path, capsys):
    """CLAUDE.md #3: an early exit before any click leaves no trace on hh.ru,
    so it must not be recorded in actions -- same convention apply/bump
    already follow. button.count() != 1 is the only no-click failure path
    here (not_found is handled separately, as a success).
    """
    env.result = EducationDeleteResult(
        ENTRY_ID, "primary", False, "кнопка удаления записи не подтверждена однозначно"
    )
    assert cmd.run(_args(tmp_path, force=True)) is True
    assert "[FAIL]" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute("SELECT action FROM actions WHERE resume_id = ?", (ENTRY_ID,)).fetchone()
    assert row is None


def test_not_found_retry_clears_a_prior_unresolved_uncertain_marker(env, tmp_path):
    """#802 vs #480 end-to-end: an uncertain marker from a prior run must not
    become a permanent block once a retry structurally confirms the entry is
    gone. Unlike delete-resume (#464/#480), this command has NO pre-flight
    has_unresolved_uncertain guard (see the module docstring in
    commands/delete_education_entry.py) -- the retry is allowed straight
    through to delete_education_entry_on_hh's own route re-check, which
    resolves it to not_found=True with no second click.
    """
    history = History(tmp_path / "history.db")
    history.record_action(
        ENTRY_ID, ENTRY_ID, "delete_education_entry", "uncertain", "клик мог уйти"
    )
    assert history.has_unresolved_uncertain(ENTRY_ID, "delete_education_entry")

    env.result = EducationDeleteResult(
        ENTRY_ID, "primary", True, "запись не найдена; уже отсутствует", not_found=True
    )
    assert cmd.run(_args(tmp_path, force=True)) is False

    assert not history.has_unresolved_uncertain(ENTRY_ID, "delete_education_entry")


def test_sigterm_after_destructive_click_leaves_unresolved_uncertain_marker(
    env, tmp_path, monkeypatch
):
    """Mirrors delete-resume's #470 guard: SIGTERM right after the destructive
    click must not let a blind retry attempt a second deletion without first
    re-checking the route.
    """

    def raising_delete(page, kind, entry_id, dry_run, *, before_click):  # noqa: ANN001, ARG001
        before_click()
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(hhru_bot.resume_education, "delete_education_entry_on_hh", raising_delete)

    cmd.run(_args(tmp_path, force=True))

    history = History(tmp_path / "history.db")
    assert history.has_unresolved_uncertain(ENTRY_ID, "delete_education_entry"), (
        "a SIGTERM after the destructive click must leave an unresolved "
        "uncertain actions marker, or a blind retry can re-attempt deletion"
    )


def test_pre_click_launch_failure_leaves_no_uncertain_marker(env, tmp_path, monkeypatch):
    def fail_launch(*_args, **_kwargs):
        raise RuntimeError("transient launch failure")

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fail_launch)

    with pytest.raises(RuntimeError, match="transient launch failure"):
        cmd.run(_args(tmp_path, force=True))

    history = History(tmp_path / "history.db")
    assert not history.has_unresolved_uncertain(ENTRY_ID, "delete_education_entry")
    assert history.command_runs()[-1]["attempted"] == 0


def test_uncertain_retry_that_still_finds_the_entry_reserves_a_fresh_row(env, tmp_path):
    """A retry landing on a STILL-open entry (not resolved yet) goes through
    the normal click path again and reserves its own uncertain/success row
    the same way the first attempt did -- no double-submit risk, since a
    second click only happens after a fresh button.count() == 1 check on the
    live DOM.
    """
    history = History(tmp_path / "history.db")
    history.record_action(
        ENTRY_ID, ENTRY_ID, "delete_education_entry", "uncertain", "клик мог уйти"
    )

    env.result = EducationDeleteResult(ENTRY_ID, "primary", True, "запись удалена; форма закрыта")
    assert cmd.run(_args(tmp_path, force=True)) is False
    assert not history.has_unresolved_uncertain(ENTRY_ID, "delete_education_entry")

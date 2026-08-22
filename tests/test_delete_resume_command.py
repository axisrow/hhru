"""Контракт delete-resume: подтверждение, аудит и обязательный target (#293)."""

from __future__ import annotations

import argparse
import signal
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.delete_resume as cmd
import hhru_bot.delete_resume
from hhru_bot.delete_resume import DeleteResumeResult
from hhru_bot.history import History

pytestmark = pytest.mark.integration

RESUME_ID = "a" * 38


def _config(tmp_path):
    resume = SimpleNamespace(id="training", resume_id=RESUME_ID)

    def get_resume(value):
        if value != "training":
            from hhru_bot.config import ConfigError

            raise ConfigError("не найдено")
        return resume

    return SimpleNamespace(
        get_resume=get_resume, storage_state_file=tmp_path / "session.json", user_agent=None
    )


def _args(tmp_path, **overrides):
    values = dict(
        config="unused",
        history=str(tmp_path / "history.db"),
        headless=True,
        resume="training",
        dry_run=False,
        force=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = SimpleNamespace(result=DeleteResumeResult(RESUME_ID, True, "удалено"))
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _: _config(tmp_path))

    @contextmanager
    def launch(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", launch)

    def delete(page, resume, dry, *, before_click=None):  # noqa: ANN001, ARG001
        if not dry and (state.result.success or state.result.uncertain):
            before_click()
        return state.result

    monkeypatch.setattr(hhru_bot.delete_resume, "delete_resume_on_hh", delete)
    return state


def test_dry_run_writes_nothing_to_history(env, tmp_path, capsys):
    env.result = DeleteResumeResult(RESUME_ID, True, "dry-run; кнопка удаления не нажата")
    cmd.run(_args(tmp_path, dry_run=True))
    assert "[DRY-RUN]" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT action, status FROM actions WHERE resume_id = ?", (RESUME_ID,)
        ).fetchone()
    assert row is None


def test_no_flags_is_dry_run(env, tmp_path, capsys):
    env.result = DeleteResumeResult(RESUME_ID, True, "dry-run; кнопка удаления не нажата")
    cmd.run(_args(tmp_path, dry_run=False, force=False))
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_uncertain_is_audited_and_fails(env, tmp_path, capsys):
    env.result = DeleteResumeResult(RESUME_ID, False, "ошибка после клика", uncertain=True)
    assert cmd.run(_args(tmp_path, force=True)) is True
    assert "uncertain" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT status FROM actions WHERE resume_id = ?", (RESUME_ID,)
        ).fetchone()
    assert row["status"] == "uncertain"


def test_live_success_is_recorded_as_single_completed_run(env, tmp_path):
    assert cmd.run(_args(tmp_path, force=True)) is False
    run = History(tmp_path / "history.db").command_runs()[-1]
    assert (run["command"], run["status"], run["attempted"], run["success"], run["failed"]) == (
        "delete-resume",
        "completed",
        1,
        1,
        0,
    )


def test_sigterm_after_destructive_click_leaves_unresolved_uncertain_marker(
    env, tmp_path, monkeypatch
):
    """Codex cycle-review PR #470 (round 2): a SIGTERM/KeyboardInterrupt
    delivered right after delete_resume_on_hh's destructive click must not
    let a blind retry attempt a second deletion. ``except Exception`` cannot
    catch a signal-raised ``BaseException``, so today no uncertain actions
    row is written when the interrupt lands after the click already fired.
    """

    def raising_delete(page, resume, dry_run, *, before_click):  # noqa: ANN001, ARG001
        before_click()
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(hhru_bot.delete_resume, "delete_resume_on_hh", raising_delete)

    cmd.run(_args(tmp_path, force=True))

    history = History(tmp_path / "history.db")
    assert history.has_unresolved_uncertain(RESUME_ID, "delete_resume"), (
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
    assert not history.has_unresolved_uncertain(RESUME_ID, "delete_resume")
    assert history.command_runs()[-1]["attempted"] == 0


def test_unresolved_uncertain_blocks_retry(env, tmp_path, capsys):
    """The guard side of the same #464 fix: once an uncertain marker exists,
    a plain retry must refuse rather than silently attempt another deletion --
    mirrors publish-resume/copy-resume's existing has_unresolved_uncertain
    guard, previously missing from delete-resume entirely.
    """
    history = History(tmp_path / "history.db")
    history.record_action(RESUME_ID, RESUME_ID, "delete_resume", "uncertain", "клик мог уйти")

    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path, force=True))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "uncertain" in out

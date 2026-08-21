"""Безопасный контракт create-resume: dry-run, подтверждение и YAML-вывод."""

from __future__ import annotations

import argparse
import signal
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.create_resume as cmd
import hhru_bot.create_resume
from hhru_bot.create_resume import CreateResumeResult
from hhru_bot.history import History

pytestmark = pytest.mark.integration
NEW_ID = "b" * 38


def _args(tmp_path, **overrides):
    values = dict(
        config="unused.yaml",
        history=str(tmp_path / "history.db"),
        headless=True,
        area="it",
        title="Backend developer",
        force=False,
        dry_run=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _: SimpleNamespace(storage_state_file=tmp_path / "session.json", user_agent=None),
    )

    @contextmanager
    def launch(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", launch)
    state = SimpleNamespace(result=CreateResumeResult(True, NEW_ID, "черновик создан"), calls=[])

    def create(page, *, area, title, dry_run):
        state.calls.append((area, title, dry_run))
        return state.result

    monkeypatch.setattr(hhru_bot.create_resume, "create_resume_on_hh", create)
    return state


def test_dry_run_is_default_and_does_not_prompt(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    cmd.run(_args(tmp_path))
    output = capsys.readouterr().out
    assert "[DRY-RUN]" in output
    assert env.calls == [("it", "Backend developer", True)]


def test_force_prints_yaml_but_does_not_modify_config(env, tmp_path, capsys):
    cmd.run(_args(tmp_path, force=True))
    output = capsys.readouterr().out
    assert f"Новый resume_id: {NEW_ID}" in output
    assert f"https://hh.ru/resume/{NEW_ID}" in output
    assert not (tmp_path / "config.yaml").exists()
    run = History(tmp_path / "history.db").command_runs()[-1]
    assert (run["command"], run["status"], run["attempted"], run["success"], run["failed"]) == (
        "create-resume",
        "completed",
        1,
        1,
        0,
    )


def test_dry_run_wins_when_force_is_also_present(env, tmp_path, capsys):
    cmd.run(_args(tmp_path, force=True, dry_run=True))
    assert "[DRY-RUN]" in capsys.readouterr().out
    assert env.calls == [("it", "Backend developer", True)]


def test_no_force_is_dry_run_even_in_non_tty(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    cmd.run(_args(tmp_path, force=False, dry_run=False))
    assert "[DRY-RUN]" in capsys.readouterr().out
    assert env.calls == [("it", "Backend developer", True)]


def test_sigterm_after_creation_leaves_unresolved_uncertain_marker(env, tmp_path, monkeypatch):
    """Codex cycle-review PR #470: a SIGTERM/KeyboardInterrupt delivered after
    ``create_resume_on_hh`` has already created the resume on hh.ru must not
    let a blind retry create a duplicate. ``run_supervised_command``'s
    ``command_runs`` ledger row (status ``interrupted``) is a diagnostic
    record, not the dedup barrier -- that's ``actions``, checked via
    ``has_unresolved_uncertain`` the same way publish-resume/copy-resume
    already do. ``except Exception`` inside ``create_resume.py::_body``
    cannot catch a signal-raised ``BaseException``, so today no ``actions``
    row is written at all when the interrupt lands after a successful
    external creation -- this test pins that gap red until fixed.
    """

    def create(page, *, area, title, dry_run):  # noqa: ANN001, ARG001
        # Simulate hh.ru having already created the resume, then the process
        # getting SIGTERM'd before the command can record anything.
        signal.raise_signal(signal.SIGTERM)
        return CreateResumeResult(True, NEW_ID, "черновик создан")

    monkeypatch.setattr(hhru_bot.create_resume, "create_resume_on_hh", create)

    cmd.run(_args(tmp_path, force=True))

    history = History(tmp_path / "history.db")
    assert history.has_unresolved_uncertain("account", "create_resume"), (
        "a SIGTERM after hh.ru already created the resume must leave an "
        "unresolved uncertain actions marker, or a blind retry can create "
        "a duplicate resume"
    )


def test_unresolved_uncertain_blocks_retry(env, tmp_path, capsys):
    """The guard side of the same #464 fix: once an uncertain marker exists
    (e.g. from the SIGTERM scenario above), a plain retry must refuse rather
    than silently attempt a second creation -- mirrors publish-resume/
    copy-resume's existing ``has_unresolved_uncertain`` guard.
    """
    history = History(tmp_path / "history.db")
    history.record_action("account", "account", "create_resume", "uncertain", "клик мог уйти")

    with pytest.raises(SystemExit):
        cmd.run(_args(tmp_path, force=True))

    output = capsys.readouterr().out
    assert "[FAIL]" in output
    assert "uncertain" in output
    # No browser call was attempted -- the guard fires before _body runs.
    assert env.calls == []

"""Безопасный контракт create-resume: dry-run, подтверждение и YAML-вывод."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.create_resume as cmd
import hhru_bot.create_resume
from hhru_bot.create_resume import CreateResumeResult

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


def test_dry_run_wins_when_force_is_also_present(env, tmp_path, capsys):
    cmd.run(_args(tmp_path, force=True, dry_run=True))
    assert "[DRY-RUN]" in capsys.readouterr().out
    assert env.calls == [("it", "Backend developer", True)]


def test_no_force_is_dry_run_even_in_non_tty(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    cmd.run(_args(tmp_path, force=False, dry_run=False))
    assert "[DRY-RUN]" in capsys.readouterr().out
    assert env.calls == [("it", "Backend developer", True)]

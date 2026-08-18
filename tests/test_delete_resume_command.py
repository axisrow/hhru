"""Контракт delete-resume: подтверждение, аудит и обязательный target (#293)."""

from __future__ import annotations

import argparse
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
    monkeypatch.setattr(
        hhru_bot.delete_resume, "delete_resume_on_hh", lambda page, resume, dry: state.result
    )
    return state


def test_dry_run_is_audited_without_force(env, tmp_path, capsys):
    env.result = DeleteResumeResult(RESUME_ID, True, "dry-run; кнопка удаления не нажата")
    cmd.run(_args(tmp_path, dry_run=True))
    assert "[DRY-RUN]" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT action, status FROM actions WHERE resume_id = ?", (RESUME_ID,)
        ).fetchone()
    assert tuple(row) == ("delete_resume", "dry_run")


def test_no_flags_is_dry_run(env, tmp_path, capsys):
    env.result = DeleteResumeResult(RESUME_ID, True, "dry-run; кнопка удаления не нажата")
    cmd.run(_args(tmp_path, dry_run=False, force=False))
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_uncertain_is_audited_and_fails(env, tmp_path, capsys):
    env.result = DeleteResumeResult(RESUME_ID, False, "ошибка после клика", uncertain=True)
    with pytest.raises(SystemExit):
        cmd.run(_args(tmp_path, force=True))
    assert "uncertain" in capsys.readouterr().out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT status FROM actions WHERE resume_id = ?", (RESUME_ID,)
        ).fetchone()
    assert row["status"] == "uncertain"

"""Durable command-run coverage for bump and the combined run command (#463)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hhru_bot.bump import BumpResult
from hhru_bot.commands import bump as bump_command
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.exit_codes import CommandExitCode
from hhru_bot.history import History

pytestmark = pytest.mark.integration


class _LaunchContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN001
        return False

    def new_page(self):
        return object()


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="letter",
        resumes=[
            ResumeConfig(
                id="resume",
                resume_url="https://hh.ru/resume/abc123",
                search=SearchFilters(text="python", area=1),
            )
        ],
    )


def _args(tmp_path: Path, *, command: str = "bump") -> argparse.Namespace:
    return argparse.Namespace(
        command=command,
        config=None,
        history=str(tmp_path / "history.db"),
        dry_run=False,
        headless=True,
        resume=None,
        max_pages=5,
        limit=1,
        approved=None,
    )


def _patch_runtime(monkeypatch, config: AppConfig) -> None:
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *_a, **_kw: _LaunchContext())
    monkeypatch.setattr("hhru_bot.throttle.Throttle.wait", lambda *_a, **_kw: None)


def test_bump_persists_success_and_correlates_action_to_run(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)
    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume",
        lambda _page, resume, _dry_run: BumpResult(resume.id, True, "ok", acted=True),
    )

    assert bump_command.run(_args(tmp_path)) is False

    history = History(tmp_path / "history.db")
    row = history.command_runs()[-1]
    assert row["command"] == "bump"
    assert (row["status"], row["attempted"], row["success"], row["failed"], row["uncertain"]) == (
        "completed",
        1,
        1,
        0,
        0,
    )
    with history._connect() as conn:
        action = conn.execute("SELECT run_id FROM actions WHERE action='bump'").fetchone()
    assert action["run_id"] == row["run_id"]
    assert "[RUN]" in capsys.readouterr().out


def test_bump_exception_still_finishes_run_and_prints_summary(
    tmp_path, monkeypatch, capsys
) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)
    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError, match="boom"):
        bump_command.run(_args(tmp_path))

    row = History(tmp_path / "history.db").command_runs()[-1]
    assert (row["status"], row["attempted"], row["failed"], row["detail"]) == (
        "failed",
        1,
        1,
        "RuntimeError: boom",
    )
    assert "[RUN]" in capsys.readouterr().out


def test_next_bump_run_recovers_abandoned_bump_as_orphaned(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)
    history = History(tmp_path / "history.db")
    abandoned = history.start_command_run(command="bump", requested_limit=None)
    monkeypatch.setattr("hhru_bot.history._pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    assert bump_command.run(_args(tmp_path)) is CommandExitCode.SIGINT

    rows = {row["run_id"]: row for row in History(tmp_path / "history.db").command_runs()}
    assert rows[abandoned]["status"] == "orphaned"
    interrupted = next(row for row in rows.values() if row["run_id"] != abandoned)
    assert interrupted["status"] == "interrupted"


def test_combined_run_creates_distinct_apply_and_bump_rows(tmp_path, monkeypatch) -> None:
    from hhru_bot.commands import apply as apply_command
    from hhru_bot.commands import run as run_command

    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)

    def apply_body(_args, _config, _history, progress):
        progress.begin_attempt()
        progress.applied_count += 1
        return False

    monkeypatch.setattr(apply_command, "_run", apply_body)
    monkeypatch.setattr(
        "hhru_bot.bump.bump_resume",
        lambda _page, resume, _dry_run: BumpResult(resume.id, True, "ok", acted=True),
    )

    assert run_command.run(_args(tmp_path, command="run")) is False

    rows = History(tmp_path / "history.db").command_runs()
    assert [row["command"] for row in rows] == ["apply", "bump"]
    assert rows[0]["run_id"] != rows[1]["run_id"]

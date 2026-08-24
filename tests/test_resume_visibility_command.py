"""Safety contract for resume-visibility (#566)."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.resume_visibility as cmd
from hhru_bot.cli import build_parser
from hhru_bot.history import History

pytestmark = pytest.mark.integration

RESUME_ID = "a" * 38


def _config(tmp_path):
    resume = SimpleNamespace(id="python", resume_id=RESUME_ID)
    return SimpleNamespace(
        get_resume=lambda value: resume,
        storage_state_file=tmp_path / "session.json",
        user_agent=None,
    )


def _args(tmp_path, **overrides):
    values = dict(
        config="unused.yaml", history=str(tmp_path / "h.db"), headless=True,
        resume="python", mode="link-only", dry_run=False, force=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_parser_exposes_all_explicit_modes():
    parser = build_parser()
    args = parser.parse_args(["resume-visibility", "--resume", "x", "--mode", "whitelist"])
    assert args.mode == "whitelist"


def test_dry_run_is_preview_and_writes_no_action(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path))
    monkeypatch.setattr(hhru_bot.browser, "launch_context", lambda *a, **kw: _context())
    assert cmd.run(_args(tmp_path, dry_run=True)) is False
    assert "DRY-RUN" in capsys.readouterr().out
    assert History(tmp_path / "h.db").count_today(RESUME_ID, "resume_visibility") == 0


def test_live_write_fails_closed_before_click_or_action(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path))
    monkeypatch.setattr(hhru_bot.browser, "launch_context", lambda *a, **kw: _context())
    assert cmd.run(_args(tmp_path, force=True)) is True
    assert "не подтверждён" in capsys.readouterr().out
    assert History(tmp_path / "h.db").count_today(RESUME_ID, "resume_visibility") == 0


class _Context:
    def __enter__(self):
        return SimpleNamespace(new_page=lambda: SimpleNamespace())

    def __exit__(self, *exc):
        return False


def _context():
    return _Context()

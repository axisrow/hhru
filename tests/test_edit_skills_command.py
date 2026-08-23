"""Regression tests for the resume-scoped edit-skills command."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.commands.edit_skills as command
from hhru_bot.skills import Skill, SkillsResult

pytestmark = pytest.mark.unit


class _Page:
    url = "https://hh.ru/resume/wrong-resume"


def test_success_output_reports_added_and_existing_skills(capsys):
    result = SkillsResult(
        success=True,
        existing=("Python", "Git"),
        proposed=(Skill("Python", "advanced"), Skill("Docker", "intermediate")),
        added=("Docker",),
    )

    command._print_success("resume", result, dry_run=False)

    output = capsys.readouterr().out
    assert "навыков было 2, добавлено 1, стало 3" in output
    assert "добавлены: Docker" in output
    assert "уже были: Python" in output


def test_success_output_reports_noop_append(capsys):
    result = SkillsResult(
        success=True,
        existing=("Python",),
        proposed=(Skill("python", "advanced"),),
        added=(),
    )

    command._print_success("resume", result, dry_run=False)

    output = capsys.readouterr().out
    assert "навыков было 1, добавлено 0, стало 1" in output
    assert "уже были: python" in output
    assert "добавлены:" not in output


@contextmanager
def _launch_context(*_args, **_kwargs):
    yield SimpleNamespace(new_page=lambda: _Page())


def test_ai_planning_rejects_wrong_resume_route_before_read_or_llm(monkeypatch, capsys, tmp_path):
    resume = SimpleNamespace(id="requested", resume_id="requested")
    config = SimpleNamespace(ai=object(), storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_args: resume)
    monkeypatch.setattr("hhru_bot.browser.launch_context", _launch_context)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *_args: None)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _page: False)
    monkeypatch.setattr(
        "hhru_bot.skills.read_skills",
        lambda _page: pytest.fail("wrong-resume page was read"),
    )
    monkeypatch.setattr(
        "hhru_bot.ai.llm_client.LLMClient",
        lambda _config: pytest.fail("LLM was called before route confirmation"),
    )

    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="requested",
        mode="append",
        skill=[],
        dry_run=True,
        force=False,
        history=str(tmp_path / "history.db"),
    )
    # #465: run() now returns `failed` (bool/CommandExitCode) under the
    # durable command_run ledger instead of raising SystemExit — the
    # validation error itself is unchanged, only how the command reports it.
    assert command.run(args) is True
    assert "страница нужного резюме не подтверждена" in capsys.readouterr().out

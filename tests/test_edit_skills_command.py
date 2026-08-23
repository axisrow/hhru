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
        existing=("Python", "Git"),
        proposed=(Skill("python", "advanced"),),
        added=(),
    )

    command._print_success("resume", result, dry_run=False)

    output = capsys.readouterr().out
    assert "навыков было 2, добавлено 0, стало 2" in output
    # The resume holds "Python"; the caller typed "python".  The report must
    # name the chip read from the page, not the caller's spelling (#528).
    assert "уже были: Python" in output
    assert "уже были: python" not in output
    assert "добавлены:" not in output


def test_success_output_names_skill_as_read_from_resume(capsys):
    """An uppercase --skill still reports the chip's own spelling (#528)."""
    result = SkillsResult(
        success=True,
        existing=("Python",),
        proposed=(Skill("PYTHON", "advanced"),),
        added=(),
    )

    command._print_success("resume", result, dry_run=False)

    output = capsys.readouterr().out
    assert "уже были: Python" in output
    # The itemized line must agree with the line above it: both name the chip
    # as read off the resume, not as the caller spelled it.
    assert "  - Python [advanced] — сохранить" in output
    assert "PYTHON" not in output


def test_dry_run_output_does_not_claim_skills_were_added(capsys):
    """A cancelled dry run must not report additions in the past tense (#528)."""
    result = SkillsResult(
        success=True,
        existing=("Python",),
        proposed=(Skill("Python", "advanced"), Skill("Docker", "basic")),
        added=("Docker",),
    )

    command._print_success("resume", result, dry_run=True)

    output = capsys.readouterr().out
    assert "навыков сейчас 1, будет добавлено 1, станет 2" in output
    assert "добавлено 1, стало 2" not in output
    assert "будут добавлены: Docker" in output
    assert "[INFO] Ничего не сохранено на hh.ru." in output


def test_success_output_normalizes_internal_whitespace_in_added_chip(capsys):
    """An added chip rendered with double internal whitespace must still be
    classified as added, not already-present (#536 round 3).

    ``_print_success`` used bare ``casefold()`` while the pipeline normalizes via
    ``_skill_key`` (``" ".join(split).casefold()``); a chip "Machine  Learning"
    (double space) mismatched the single-spaced plan key and was reported as
    "уже были" / "сохранить" instead of "добавлены" / "добавить".
    """
    result = SkillsResult(
        success=True,
        existing=("Python",),
        proposed=(Skill("Machine Learning", "intermediate"),),
        added=("Machine  Learning",),  # double space from hh.ru chip
    )

    command._print_success("resume", result, dry_run=False)

    output = capsys.readouterr().out
    # The chip spelling observed on hh.ru is preserved in the roll-up.
    assert "добавлены: Machine  Learning" in output
    # The proposed skill is classified as an addition despite the whitespace gap.
    assert "добавить" in output
    assert "сохранить" not in output
    assert "уже были: Machine" not in output


def test_success_output_normalizes_internal_whitespace_in_existing_chip(capsys):
    """An existing chip with double internal whitespace must match a same-spelling
    plan entry as already-present (#536 round 3).

    Without normalizing the existing-chip key, "Python  Dev" (double space) would
    not match the plan's "Python Dev" (single space); the skill would fall through
    to the ``skill.name`` fallback and be reported as an addition.
    """
    result = SkillsResult(
        success=True,
        existing=("Python  Dev",),  # double space from hh.ru chip
        proposed=(Skill("Python Dev", "advanced"),),  # single space from plan
        added=(),
    )

    command._print_success("resume", result, dry_run=False)

    output = capsys.readouterr().out
    assert "уже были: Python  Dev" in output  # chip spelling preserved
    assert "  - Python  Dev [advanced] — сохранить" in output
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

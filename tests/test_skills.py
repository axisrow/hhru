"""Pure safety and parsing tests for resume skills (#263)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import hhru_bot.skills as skills_module
from hhru_bot.config import bare_resume
from hhru_bot.skills import (
    Skill,
    build_skills_prompt,
    edit_skills_on_hh,
    parse_manual_skills,
    parse_skill_plan,
)

pytestmark = pytest.mark.unit


def test_parse_skill_plan_accepts_fenced_json_and_levels() -> None:
    result = parse_skill_plan('```json\n[{"name":" Python ","level":"advanced"}]\n```')
    assert result[0].name == "Python"
    assert result[0].level == "advanced"


@pytest.mark.parametrize(
    "payload",
    [
        '[{"name": "Python", "level": "expert"}]',
        '[{"name": "Python", "level": "advanced"}, {"name": "python", "level": "basic"}]',
        '{"name": "Python", "level": "advanced"}',
    ],
)
def test_parse_skill_plan_rejects_unsafe_shape(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_skill_plan(payload)


def test_manual_skills_are_structured() -> None:
    assert parse_manual_skills(["Python=advanced", "Git=intermediate"])[1].name == "Git"


def test_prompt_mentions_existing_skills_and_mode() -> None:
    prompt = build_skills_prompt("Python backend", ("Git",), "append")
    assert "Git" in prompt[1]["content"]
    assert "до-заполнения" in prompt[1]["content"]


def test_edit_skills_waits_for_dedicated_editor_route(monkeypatch) -> None:
    """#328: skills editor is mounted only after the keySkills route commits."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    trigger = MagicMock()
    trigger.count.return_value = 1
    editor = MagicMock()
    editor.input_value.return_value = ""
    cancel = MagicMock()
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_INPUT: editor,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_CANCEL: cancel,
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "read_skills", lambda _page: ())

    result = edit_skills_on_hh(
        page, resume, (Skill("Python", "advanced"),), dry_run=True, mode="append"
    )

    assert result.success is True
    page.wait_for_url.assert_called_once_with(
        "**/resume/edit/resume-id/keySkills", wait_until="commit"
    )
    editor.wait_for.assert_called_once_with(state="visible")

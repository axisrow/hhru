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


def test_edit_skills_retries_pre_hydration_noop_click(monkeypatch) -> None:
    """#337: a visible SSR trigger can receive a no-op click before hydration."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 1
    editor = MagicMock()
    editor.input_value.return_value = ""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    editor.wait_for.side_effect = [PlaywrightTimeoutError("not hydrated"), None]
    cancel = MagicMock()

    def click_side_effect():
        # The second attempt's click is the one that actually hydrates and
        # commits the dedicated edit route.
        if trigger.click.call_count == 2:
            page.url = "https://hh.ru/resume/edit/resume-id/keySkills"

    trigger.click.side_effect = click_side_effect
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
    assert trigger.click.call_count == 2
    assert editor.wait_for.call_count == 2
    editor.wait_for.assert_called_with(state="visible", timeout=30_000)
    page.wait_for_url.assert_not_called()


def test_edit_skills_does_not_retry_after_navigation_to_editor(monkeypatch) -> None:
    """A slow editor mount after navigation is not a hydration no-op (#337)."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 1
    editor = MagicMock()
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    def navigate_then_timeout(**_kwargs):
        page.url = "https://hh.ru/resume/edit/resume-id/keySkills"
        raise PlaywrightTimeoutError("editor is still mounting")

    editor.wait_for.side_effect = navigate_then_timeout
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_INPUT: editor,
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)

    result = edit_skills_on_hh(
        page, resume, (Skill("Python", "advanced"),), dry_run=True, mode="append"
    )

    assert result.success is False
    assert trigger.click.call_count == 1


def test_edit_skills_rejects_editor_on_wrong_resume_route(monkeypatch) -> None:
    """#337 follow-up: a visible editor must belong to the requested resume_id.

    The pre-#337 code enforced this via ``wait_for_url`` before querying the
    editor. Dropping that wait must not drop the invariant it protected: a
    visible editor on an unexpected edit route must still fail closed instead
    of reading/writing skills into the wrong resume.
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    # The editor is visible, but the committed route belongs to a different resume.
    page.url = "https://hh.ru/resume/edit/other-resume-id/keySkills"
    trigger = MagicMock()
    trigger.count.return_value = 1
    editor = MagicMock()
    editor.wait_for.return_value = None
    cancel = MagicMock()
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_INPUT: editor,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_CANCEL: cancel,
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    read_skills = MagicMock(return_value=())
    monkeypatch.setattr(skills_module, "read_skills", read_skills)

    result = edit_skills_on_hh(
        page, resume, (Skill("Python", "advanced"),), dry_run=True, mode="append"
    )

    assert result.success is False
    assert result.reason == "форма навыков открыта не для того резюме"
    read_skills.assert_not_called()
    cancel.click.assert_not_called()


def test_edit_skills_accepts_correct_edit_route_on_first_attempt(monkeypatch) -> None:
    """Positive counterpart: the post-condition must accept the real edit route.

    Pins the check against the resume's ``keySkills`` edit path specifically
    — a check comparing against the profile path (or any other constant)
    instead would either reject this legitimate first-attempt success or
    accept the wrong-route case above, so this test and that one must both
    pass only together with the correct comparison.
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/resume-id/keySkills"
    trigger = MagicMock()
    trigger.count.return_value = 1
    editor = MagicMock()
    editor.wait_for.return_value = None
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
    assert trigger.click.call_count == 1
    assert editor.wait_for.call_count == 1


def _mock_chip_locator() -> MagicMock:
    """A RESUME_SKILLS_CHIP locator stub whose .nth().wait_for() no-ops."""
    chip_locator = MagicMock()
    chip_locator.nth.return_value.wait_for.return_value = None
    return chip_locator


def test_edit_skills_reports_only_chips_observed_after_save(monkeypatch) -> None:
    """A closed editor is not enough: the saved chip set must match the plan."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    save = MagicMock()
    save.count.return_value = 1
    chip_locator = _mock_chip_locator()
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: chip_locator,
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    read_skills = MagicMock(side_effect=[("Python",), ("Python", "Docker")])
    monkeypatch.setattr(skills_module, "read_skills", read_skills)

    result = edit_skills_on_hh(
        page, resume, (Skill("Docker", "intermediate"),), dry_run=False, mode="append"
    )

    assert result.success is True
    assert result.added == ("Docker",)
    assert result.acted is True
    assert read_skills.call_count == 2
    # #536 round 1: the post-save read must wait for the chip container to
    # settle instead of racing the React re-render right after the editor
    # (a separate overlay) reports hidden.
    chip_locator.nth.assert_called_once_with(1)
    chip_locator.nth.return_value.wait_for.assert_called_once_with(state="visible", timeout=5_000)


def test_edit_skills_marks_rejected_chip_as_uncertain(monkeypatch) -> None:
    """A successful save click with a missing chip must not produce [OK]."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    save = MagicMock()
    save.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: _mock_chip_locator(),
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    monkeypatch.setattr(
        skills_module, "read_skills", MagicMock(side_effect=[("Python",), ("Python",)])
    )

    result = edit_skills_on_hh(
        page, resume, (Skill("Docker", "intermediate"),), dry_run=False, mode="append"
    )

    assert result.success is False
    assert result.acted is True
    assert "не совпало с планом" in result.reason


def test_edit_skills_post_save_wait_timeout_falls_through_to_strict_read(monkeypatch) -> None:
    """If the chip never settles in time, the strict multiset check still fires
    (fail-closed) instead of the wait's PlaywrightError propagating uncaught."""
    from playwright.sync_api import Error as PlaywrightError

    resume = bare_resume("resume-id")
    page = MagicMock()
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    save = MagicMock()
    save.count.return_value = 1
    chip_locator = MagicMock()
    chip_locator.nth.return_value.wait_for.side_effect = PlaywrightError("timeout")
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: chip_locator,
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    monkeypatch.setattr(
        skills_module, "read_skills", MagicMock(side_effect=[("Python",), ("Python",)])
    )

    result = edit_skills_on_hh(
        page, resume, (Skill("Docker", "intermediate"),), dry_run=False, mode="append"
    )

    assert result.success is False
    assert result.acted is True
    assert "не совпало с планом" in result.reason


def test_edit_skills_normalizes_internal_whitespace_in_observed_chips(monkeypatch) -> None:
    """A chip rendered with double internal whitespace must still match the plan.

    parse_skill_plan normalizes planned names via " ".join(split); read_skills only
    strips. Without applying the same normalization to observed/existing chips, a
    chip carrying a double space (or nbsp) would falsely mismatch the Counter and
    report false uncertain, locking the resume via has_unresolved_uncertain (#536
    round 2). The raw spelling is still preserved in the success report.
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    save = MagicMock()
    save.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: _mock_chip_locator(),
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    # hh.ru rendered the added chip with a double internal space; the plan carries
    # a single space (parse_skill_plan normalized it).
    monkeypatch.setattr(
        skills_module,
        "read_skills",
        MagicMock(side_effect=[("Python",), ("Python", "Machine  Learning")]),
    )

    result = edit_skills_on_hh(
        page, resume, (Skill("Machine Learning", "intermediate"),), dry_run=False, mode="append"
    )

    assert result.success is True
    assert result.acted is True
    # Spelling observed on hh.ru is preserved in the success report.
    assert result.added == ("Machine  Learning",)


def test_edit_skills_dedups_existing_chip_with_internal_whitespace(monkeypatch) -> None:
    """An existing chip rendered with double internal whitespace must dedup the
    same skill from the plan, not be treated as a new addition.

    Without normalizing the existing-chip key (line 162), "Python  Dev" (double
    space) would not match the plan's "Python Dev" (single space), the skill would
    be re-added as a duplicate, the post-save Counter would mismatch, and the
    resume would lock via has_unresolved_uncertain (#536 round 2).
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    save = MagicMock()
    save.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: _mock_chip_locator(),
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    # Existing chip already carries a double space; the plan re-offers the same
    # skill with a single space — it must be deduped, not re-added.
    monkeypatch.setattr(
        skills_module,
        "read_skills",
        MagicMock(side_effect=[("Python  Dev",), ("Python  Dev",)]),
    )

    result = edit_skills_on_hh(
        page, resume, (Skill("Python Dev", "advanced"),), dry_run=False, mode="append"
    )

    assert result.success is True
    assert result.acted is True
    assert result.added == ()

"""Pure safety and parsing tests for resume skills (#263)."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

import hhru_bot.skills as skills_module
from hhru_bot.config import bare_resume
from hhru_bot.skills import (
    Skill,
    _confirm_skill_levels,
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
    """A RESUME_SKILLS_CHIP (editor combobox) locator stub for the fill+click loop.

    #801: .filter(has_text=...) resolves immediately with one match, so the
    post-fill+click commit-wait loop in edit_skills_on_hh does not poll until
    CHIP_COMMIT_TIMEOUT_MS on every addition. #813: the post-save settle-wait
    now targets RESUME_SKILLS_DISPLAY_TAG instead (see _mock_display_tag_locator),
    not this locator's .nth().wait_for().
    """
    chip_locator = MagicMock()
    chip_locator.filter.return_value.count.return_value = 1
    return chip_locator


def _mock_suggest_locator() -> MagicMock:
    """A RESUME_SKILLS_SUGGEST_USER_INPUT locator stub for the fill+click loop.

    #826: Enter never commits a chip in this combobox; the code clicks the
    autocomplete option that echoes the typed text instead, via
    `.filter(has_text=...).first` (review #830: exact-text filtered, matching
    the expected_chip pattern below, to avoid clicking a stale option from a
    previous iteration). `.wait_for()`/`.click()` on the raw locator resolve
    immediately as a no-op for tests that only care about the eventual chip
    outcome, not the click mechanics themselves — assert against
    `.filter.return_value.first` when a test needs to observe the click.
    """
    suggestion = MagicMock()
    suggestion.filter.return_value.first.wait_for.return_value = None
    return suggestion


def _mock_display_tag_locator() -> MagicMock:
    """A RESUME_SKILLS_DISPLAY_TAG locator stub whose .nth().wait_for() no-ops.

    #813: the post-save settle-wait reads the resume card's own skill tags,
    not the editor's chip widget (which no longer exists once the editor closes).
    """
    tag_locator = MagicMock()
    tag_locator.nth.return_value.wait_for.return_value = None
    return tag_locator


def test_edit_skills_reports_only_chips_observed_after_save(monkeypatch) -> None:
    """A closed editor is not enough: the saved chip set must match the plan."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    input_.input_value.return_value = ""
    save = MagicMock()
    save.count.return_value = 1
    chip_locator = _mock_chip_locator()
    tag_locator = _mock_display_tag_locator()
    trigger = MagicMock()
    trigger.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT: _mock_suggest_locator(),
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: chip_locator,
        skills_module.resume_page.RESUME_SKILLS_DISPLAY_TAG: tag_locator,
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    monkeypatch.setattr(skills_module, "read_skills", MagicMock(return_value=("Python",)))
    read_display_skills = MagicMock(return_value=("Python", "Docker"))
    monkeypatch.setattr(skills_module, "read_display_skills", read_display_skills)

    result = edit_skills_on_hh(
        page, resume, (Skill("Docker", "intermediate"),), dry_run=False, mode="append"
    )

    assert result.success is True
    assert result.added == ("Docker",)
    assert result.acted is True
    read_display_skills.assert_called_once()
    # #536 round 1 / #813: the post-save read must wait for the resume card's
    # own skill tags to settle instead of racing the React re-render right
    # after the editor (a separate overlay) reports hidden.
    tag_locator.nth.assert_called_once_with(1)
    tag_locator.nth.return_value.wait_for.assert_called_once_with(state="visible", timeout=5_000)


def test_edit_skills_waits_for_each_chip_before_next_addition(monkeypatch) -> None:
    """#801: consecutive additions must each be confirmed by an exact-text chip
    (and a cleared input) before the next fill+Enter — a blind fill+Enter pair
    is what let "FastAPI" + "LangChain" merge into "FastAPILangChain"."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    input_.input_value.return_value = ""
    save = MagicMock()
    save.count.return_value = 1
    chip_locator = _mock_chip_locator()
    suggestion = _mock_suggest_locator()
    trigger = MagicMock()
    trigger.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT: suggestion,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: chip_locator,
        skills_module.resume_page.RESUME_SKILLS_DISPLAY_TAG: _mock_display_tag_locator(),
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    monkeypatch.setattr(skills_module, "read_skills", MagicMock(return_value=()))
    monkeypatch.setattr(
        skills_module, "read_display_skills", MagicMock(return_value=("FastAPI", "LangChain"))
    )

    result = edit_skills_on_hh(
        page,
        resume,
        (Skill("FastAPI", "intermediate"), Skill("LangChain", "intermediate")),
        dry_run=False,
        mode="append",
    )

    assert result.success is True
    assert result.added == ("FastAPI", "LangChain")
    assert input_.fill.call_args_list == [call("FastAPI"), call("LangChain")]
    # #826: each addition is committed by clicking the autocomplete option
    # that echoes the typed text, not by pressing Enter.
    filter_calls = [c.kwargs["has_text"].pattern for c in suggestion.filter.call_args_list]
    assert filter_calls == ["^FastAPI$", "^LangChain$"]
    assert suggestion.filter.return_value.first.click.call_count == 2
    # Each addition is confirmed by its own exact-text filter, not a shared
    # chip-count check that a merged chip would also satisfy.
    filter_calls = [c.kwargs["has_text"].pattern for c in chip_locator.filter.call_args_list]
    assert filter_calls == ["^FastAPI$", "^LangChain$"]


def test_edit_skills_stops_input_after_chip_commit_timeout(monkeypatch) -> None:
    """#801: if a chip never confirms, further additions must not be typed —
    the resulting mismatch is left for the post-save Counter check to catch."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    input_.input_value.return_value = ""
    save = MagicMock()
    save.count.return_value = 1
    chip_locator = MagicMock()
    # The expected chip never appears — simulates a merged/rejected chip.
    chip_locator.filter.return_value.count.return_value = 0
    trigger = MagicMock()
    trigger.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT: _mock_suggest_locator(),
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: chip_locator,
        skills_module.resume_page.RESUME_SKILLS_DISPLAY_TAG: _mock_display_tag_locator(),
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    monkeypatch.setattr(skills_module, "read_skills", MagicMock(return_value=()))
    monkeypatch.setattr(
        skills_module, "read_display_skills", MagicMock(return_value=("FastAPILangChain",))
    )
    monkeypatch.setattr(skills_module.time, "monotonic", MagicMock(side_effect=range(10_000)))
    monkeypatch.setattr(page, "wait_for_timeout", MagicMock())

    result = edit_skills_on_hh(
        page,
        resume,
        (Skill("FastAPI", "intermediate"), Skill("LangChain", "intermediate")),
        dry_run=False,
        mode="append",
    )

    assert result.success is False
    assert result.acted is True
    # Only the first skill was typed; the timeout stopped the loop before the
    # second fill+Enter could race the still-unsettled first one.
    assert input_.fill.call_args_list == [call("FastAPI")]


def test_edit_skills_marks_rejected_chip_as_uncertain(monkeypatch) -> None:
    """A successful save click with a missing chip must not produce [OK]."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    input_.input_value.return_value = ""
    save = MagicMock()
    save.count.return_value = 1
    trigger = MagicMock()
    trigger.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT: _mock_suggest_locator(),
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: _mock_chip_locator(),
        skills_module.resume_page.RESUME_SKILLS_DISPLAY_TAG: _mock_display_tag_locator(),
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    monkeypatch.setattr(skills_module, "read_skills", MagicMock(return_value=("Python",)))
    monkeypatch.setattr(skills_module, "read_display_skills", MagicMock(return_value=("Python",)))

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
    page.url = "https://hh.ru/resume/resume-id"
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    input_.input_value.return_value = ""
    save = MagicMock()
    save.count.return_value = 1
    tag_locator = MagicMock()
    tag_locator.nth.return_value.wait_for.side_effect = PlaywrightError("timeout")
    trigger = MagicMock()
    trigger.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT: _mock_suggest_locator(),
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: _mock_chip_locator(),
        skills_module.resume_page.RESUME_SKILLS_DISPLAY_TAG: tag_locator,
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    monkeypatch.setattr(skills_module, "read_skills", MagicMock(return_value=("Python",)))
    monkeypatch.setattr(skills_module, "read_display_skills", MagicMock(return_value=("Python",)))

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
    page.url = "https://hh.ru/resume/resume-id"
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    input_.input_value.return_value = ""
    save = MagicMock()
    save.count.return_value = 1
    trigger = MagicMock()
    trigger.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT: _mock_suggest_locator(),
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: _mock_chip_locator(),
        skills_module.resume_page.RESUME_SKILLS_DISPLAY_TAG: _mock_display_tag_locator(),
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    # hh.ru rendered the added chip with a double internal space; the plan carries
    # a single space (parse_skill_plan normalized it).
    monkeypatch.setattr(skills_module, "read_skills", MagicMock(return_value=("Python",)))
    monkeypatch.setattr(
        skills_module,
        "read_display_skills",
        MagicMock(return_value=("Python", "Machine  Learning")),
    )

    result = edit_skills_on_hh(
        page, resume, (Skill("Machine Learning", "intermediate"),), dry_run=False, mode="append"
    )

    assert result.success is True
    assert result.acted is True
    # Spelling observed on hh.ru is preserved in the success report.
    assert result.added == ("Machine  Learning",)


def test_edit_skills_accepts_edit_route_with_query(monkeypatch) -> None:
    """Query parameters on the edit route must not break the route guard."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/resume-id/keySkills?foo=bar"
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


def test_edit_skills_rejects_wrong_route_with_empty_resume_id(monkeypatch) -> None:
    """An empty resume_id must not accidentally match a different edit route."""
    resume = bare_resume("")
    page = MagicMock()
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


def test_edit_skills_dedups_existing_chip_with_internal_whitespace(monkeypatch) -> None:
    """An existing chip rendered with double internal whitespace must dedup the
    same skill from the plan, not be treated as a new addition.

    Without normalizing the existing-chip key (line 162), "Python  Dev" (double
    space) would not match the plan's "Python Dev" (single space); the skill would
    be re-added as a duplicate, the post-save Counter would mismatch, and the
    resume would lock via has_unresolved_uncertain (#536 round 2).
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    editor = MagicMock()
    editor.wait_for.return_value = None
    input_ = MagicMock()
    input_.count.return_value = 1
    input_.input_value.return_value = ""
    save = MagicMock()
    save.count.return_value = 1
    trigger = MagicMock()
    trigger.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
        skills_module.resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT: _mock_suggest_locator(),
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
        skills_module.resume_page.RESUME_SKILLS_CHIP: _mock_chip_locator(),
        skills_module.resume_page.RESUME_SKILLS_DISPLAY_TAG: _mock_display_tag_locator(),
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    # Existing chip already carries a double space; the plan re-offers the same
    # skill with a single space — it must be deduped, not re-added.
    monkeypatch.setattr(skills_module, "read_skills", MagicMock(return_value=("Python  Dev",)))
    monkeypatch.setattr(
        skills_module, "read_display_skills", MagicMock(return_value=("Python  Dev",))
    )

    result = edit_skills_on_hh(
        page, resume, (Skill("Python Dev", "advanced"),), dry_run=False, mode="append"
    )

    assert result.success is True
    assert result.acted is True
    assert result.added == ()


def test_edit_skills_opens_editor_via_resume_scoped_route_on_empty_resume(monkeypatch) -> None:
    """#789/#787: when the regular skills-add button is absent, the code must
    navigate directly to /resume/edit/{id}/keySkills instead of clicking a
    fallback suggest-item."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 0
    editor = MagicMock()
    editor.wait_for.return_value = None
    cancel = MagicMock()
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
        skills_module.resume_page.RESUME_SKILLS_INPUT: editor,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_CANCEL: cancel,
    }[selector]
    goto_calls = []

    def fake_goto(p, url):
        goto_calls.append(url)
        page.url = url

    monkeypatch.setattr(skills_module, "goto_hh", fake_goto)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "read_skills", lambda _page: ())

    result = edit_skills_on_hh(
        page, resume, (Skill("Python", "advanced"),), dry_run=True, mode="append"
    )

    assert result.success is True
    assert len(goto_calls) == 2
    assert goto_calls[0].endswith("/resume/resume-id")
    assert goto_calls[1].endswith("/resume/edit/resume-id/keySkills")
    trigger.click.assert_not_called()


def test_edit_skills_navigation_timeout_on_empty_resume_returns_failure(monkeypatch) -> None:
    """#789/#787: a PlaywrightError during direct navigation must fail closed."""
    from playwright.sync_api import Error as PlaywrightError

    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 0
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
    }[selector]
    goto_attempt = 0

    def fake_goto(_page, url):
        nonlocal goto_attempt
        goto_attempt += 1
        if goto_attempt == 2:
            raise PlaywrightError("navigation failed")

    monkeypatch.setattr(skills_module, "goto_hh", fake_goto)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)

    result = edit_skills_on_hh(
        page, resume, (Skill("Python", "advanced"),), dry_run=True, mode="append"
    )

    assert result.success is False
    assert "форма навыков не открылась" in result.reason


def test_edit_skills_wrong_route_after_navigation_on_empty_resume_returns_failure(
    monkeypatch,
) -> None:
    """#789/#787: if the committed URL does not match the resume-scoped path,
    the command must fail closed instead of operating on an unrelated editor."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/profile/edit/keySkills"
    trigger = MagicMock()
    trigger.count.return_value = 0
    page.locator.side_effect = lambda selector: {
        skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
    }[selector]
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)

    result = edit_skills_on_hh(
        page, resume, (Skill("Python", "advanced"),), dry_run=True, mode="append"
    )

    assert result.success is False
    assert "форма навыков открыта не для того резюме" in result.reason


def test_edit_skills_editor_wait_timeout_on_empty_resume_returns_failure(monkeypatch) -> None:
    """#789/#787: if the editor never becomes visible after direct navigation,
    the command must fail closed."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/resume-id/keySkills"
    trigger = MagicMock()
    trigger.count.return_value = 0
    editor = MagicMock()
    editor.wait_for.side_effect = PlaywrightTimeoutError("editor hidden")
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
    assert "форма навыков не открылась" in result.reason


def test_confirm_skill_levels_noop_when_not_on_levels_step() -> None:
    """The first save can return straight to the resume card (no pending
    levels) — this must be a silent no-op, not a false failure (#813)."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"

    assert _confirm_skill_levels(page, (Skill("Docker", "intermediate"),)) is None
    page.locator.assert_not_called()


def test_confirm_skill_levels_clicks_matching_radio_and_saves() -> None:
    """#813: each addition's level radio (name={skill}{Russian label}) is
    clicked, then the step's own Save is clicked and awaited hidden."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/resume-id/skillsLevels?fromBlock=keySkills"
    radio = MagicMock()
    radio.count.return_value = 1
    save = MagicMock()
    save.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        "input[name='SeleniumСредний']": radio,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
    }[selector]

    error = _confirm_skill_levels(page, (Skill("Selenium", "intermediate"),))

    assert error is None
    radio.click.assert_called_once_with(
        force=True, timeout=skills_module.SKILLS_LEVELS_STEP_TIMEOUT_MS
    )
    save.click.assert_called_once()
    save.wait_for.assert_called_once_with(
        state="hidden", timeout=skills_module.SKILLS_LEVELS_STEP_TIMEOUT_MS
    )


def test_confirm_skill_levels_skips_missing_radio_without_failing() -> None:
    """A name hh.ru silently absorbed elsewhere may have no radio group here —
    that alone must not fail the step; the caller's Counter check still
    catches a skill that never actually landed."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/resume-id/skillsLevels?fromBlock=keySkills"
    missing_radio = MagicMock()
    missing_radio.count.return_value = 0
    save = MagicMock()
    save.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        "input[name='SeleniumСредний']": missing_radio,
        skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
    }[selector]

    error = _confirm_skill_levels(page, (Skill("Selenium", "intermediate"),))

    assert error is None
    missing_radio.click.assert_not_called()
    save.click.assert_called_once()


def test_confirm_skill_levels_reports_missing_save_button() -> None:
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/resume-id/skillsLevels?fromBlock=keySkills"
    save = MagicMock()
    save.count.return_value = 0
    page.locator.return_value = save

    error = _confirm_skill_levels(page, ())

    assert error == "кнопка сохранения уровней навыков не найдена однозначно"


def test_edit_skills_handles_levels_wizard_step_for_new_skill(monkeypatch) -> None:
    """End-to-end #813 regression: a brand-new skill routes through the
    skillsLevels step, and the post-save read must see it once that step's
    own Save completes — not the zero the pre-fix code observed."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    editor = MagicMock()
    input_ = MagicMock()
    input_.count.return_value = 1
    input_.input_value.return_value = ""
    save = MagicMock()
    save.count.return_value = 1
    chip_locator = _mock_chip_locator()
    tag_locator = _mock_display_tag_locator()
    levels_radio = MagicMock()
    levels_radio.count.return_value = 1
    trigger = MagicMock()
    trigger.count.return_value = 1

    # editor.wait_for(state="hidden") succeeds (first save closed the editor);
    # the URL only flips to the levels step once that click actually happens.
    def save_click_side_effect():
        page.url = "https://hh.ru/resume/edit/resume-id/skillsLevels?fromBlock=keySkills"

    save.click.side_effect = save_click_side_effect

    def locate(selector):
        if selector == "input[name='SeleniumСредний']":
            return levels_radio
        return {
            skills_module.resume_page.RESUME_SKILLS_EDIT_BUTTON: trigger,
            skills_module.resume_page.RESUME_SKILLS_CHIP_INPUT: input_,
            skills_module.resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT: _mock_suggest_locator(),
            skills_module.resume_page.RESUME_PARTIAL_EDIT_SAVE: save,
            skills_module.resume_page.RESUME_SKILLS_CHIP: chip_locator,
            skills_module.resume_page.RESUME_SKILLS_DISPLAY_TAG: tag_locator,
        }[selector]

    page.locator.side_effect = locate
    monkeypatch.setattr(skills_module, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(skills_module, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(skills_module, "has_login_form", lambda _page: False)
    monkeypatch.setattr(skills_module, "open_hydrated_resume_editor", lambda *_a, **_kw: editor)
    monkeypatch.setattr(skills_module, "read_skills", MagicMock(return_value=()))
    monkeypatch.setattr(skills_module, "read_display_skills", MagicMock(return_value=("Selenium",)))

    result = edit_skills_on_hh(
        page, resume, (Skill("Selenium", "intermediate"),), dry_run=False, mode="append"
    )

    assert result.success is True
    assert result.added == ("Selenium",)
    levels_radio.click.assert_called_once_with(
        force=True, timeout=skills_module.SKILLS_LEVELS_STEP_TIMEOUT_MS
    )
    # Save is clicked twice: once for the keySkills editor, once for the
    # levels step this test exercises.
    assert save.click.call_count == 2

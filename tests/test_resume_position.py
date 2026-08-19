from unittest.mock import MagicMock

import pytest

import hhru_bot.resume_position as resume_position
from hhru_bot.config import bare_resume
from hhru_bot.resume_position import (
    PositionValues,
    build_position_prompt,
    fill_only_missing,
    parse_position_response,
)

pytestmark = pytest.mark.unit


def test_position_response_accepts_structured_values_without_inventing_salary():
    plan = parse_position_response(
        '{"title":"Backend engineer","salary":null,"currency":null,'
        '"specializations":[],"employment":["full_time"],'
        '"work_format":["remote"],"commute":"no_limit","business_trips":false}'
    )
    assert plan == PositionValues(
        title="Backend engineer",
        salary=None,
        currency=None,
        specializations=[],
        employment=["full_time"],
        work_format=["remote"],
        commute="no_limit",
        business_trips=False,
    )


def test_position_response_rejects_unknown_enum():
    with pytest.raises(ValueError, match="employment enum"):
        parse_position_response('{"employment":["whatever"]}')


def test_position_response_rejects_non_string_title():
    with pytest.raises(ValueError, match="title"):
        parse_position_response('{"title":{"role":"Backend"}}')


def test_prompt_contains_mode_and_current_values():
    messages = build_position_prompt(
        type("Profile", (), {"desired_role": "Python developer", "skills": ["Python"]})(),
        PositionValues(title="Existing", salary=None),
        "fill",
    )
    assert messages[0]["role"] == "system"
    assert "salary" in messages[0]["content"]
    assert '"mode": "fill"' in messages[1]["content"]
    assert '"Existing"' in messages[1]["content"]


def test_fill_mode_preserves_existing_values():
    current = PositionValues(title="Existing", employment=["full_time"], business_trips=False)
    plan = PositionValues(
        title="New",
        salary=100000,
        employment=["remote"],
        commute="up_to_1_hour",
        business_trips=True,
    )
    merged = fill_only_missing(current, plan)
    assert merged.title == ""
    assert merged.employment is None
    assert merged.business_trips is None
    assert merged.salary == 100000


def test_open_position_form_retries_pre_hydration_noop_click(monkeypatch):
    """#337: an SSR anchor has no handler until hydration, and URL stays put."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    edit = MagicMock()
    edit.count.return_value = 1
    form = MagicMock()
    form.count.return_value = 0
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    form.wait_for.side_effect = [
        PlaywrightTimeoutError("not hydrated"),
        None,
    ]

    def click_side_effect():
        # The second attempt's click is the one that actually hydrates and
        # commits the dedicated edit route.
        if edit.click.call_count == 2:
            page.url = "https://hh.ru/resume/edit/resume-id/position"

    edit.click.side_effect = click_side_effect
    page.locator.side_effect = lambda selector: {
        resume_position.EDIT: edit,
        resume_position.FORM: form,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_position, "read_display_position", lambda _page: PositionValues())
    monkeypatch.setattr(resume_position, "read_position", lambda _page: PositionValues())

    resume_position.open_position_form(page, resume)

    assert edit.click.call_count == 2
    assert form.wait_for.call_count == 2
    form.wait_for.assert_called_with(state="visible", timeout=30_000)
    page.wait_for_url.assert_not_called()


def test_open_position_form_rejects_form_on_wrong_resume_route(monkeypatch):
    """#337 follow-up: a visible form must belong to the requested resume_id.

    The pre-#337 code enforced this via ``wait_for_url`` before querying the
    form. Dropping that wait must not drop the invariant it protected: a
    visible ``FORM`` on an unexpected edit route (e.g. hh.ru routed the click
    to a different resume) must still fail closed instead of being read as
    the requested resume's position.
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    # The form is visible, but the committed route belongs to a different resume.
    page.url = "https://hh.ru/resume/edit/other-resume-id/position"
    edit = MagicMock()
    edit.count.return_value = 1
    form = MagicMock()
    form.count.return_value = 0
    form.wait_for.return_value = None
    page.locator.side_effect = lambda selector: {
        resume_position.EDIT: edit,
        resume_position.FORM: form,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "has_login_form", lambda _page: False)
    monkeypatch.setattr(resume_position, "read_display_position", lambda _page: PositionValues())
    monkeypatch.setattr(resume_position, "read_position", lambda _page: PositionValues())

    with pytest.raises(
        RuntimeError, match="форма редактирования позиции открыта не для того резюме"
    ):
        resume_position.open_position_form(page, resume)

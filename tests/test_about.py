"""Pure tests for the LLM-assisted inline about-section flow."""

from unittest.mock import MagicMock

import pytest

import hhru_bot.about as about_module
from hhru_bot.about import (
    AboutGenerationError,
    build_about_prompt,
    generate_about,
    open_about_editor,
)
from hhru_bot.commands.about import draft_prefix
from hhru_bot.config import bare_resume

pytestmark = pytest.mark.unit


class Response:
    def __init__(self, content):
        self.content = content


class Client:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.messages = None

    def chat(self, messages, **kwargs):
        self.messages = messages
        if self.error:
            raise self.error
        return Response(self.content)


def test_empty_about_is_generated_from_profile():
    profile = type(
        "Profile",
        (),
        {"summary": "Инженер", "desired_role": "Аналитик", "skills": ["SQL"], "highlights": []},
    )()
    client = Client("Люблю разбираться в сложных задачах.")

    draft = generate_about(client, "", profile)

    assert draft.mode == "с нуля"
    assert draft.text == "Люблю разбираться в сложных задачах."
    assert "Сформируй самопрезентацию с нуля" in client.messages[1]["content"]


def test_fill_mode_preserves_existing_text():
    client = Client("Готовлюсь к переходу в data engineering.")
    draft = generate_about(client, "Уже написано.", None)

    assert draft.mode == "до-заполнение"
    assert draft.text.startswith("Уже написано.\n\n")
    assert "Существующий текст нельзя переписывать" in client.messages[1]["content"]


def test_empty_llm_response_uses_profile_summary_fallback():
    profile = type("Profile", (), {"summary": "Резервный текст"})()
    draft = generate_about(Client("  "), "", profile)
    assert draft.text == "Резервный текст"
    assert draft.source == "fallback"


def test_llm_error_preserves_existing_text():
    draft = generate_about(Client(error=RuntimeError("offline")), "Не менять.", None)
    assert draft.text == "Не менять."
    assert draft.source == "fallback"


def test_llm_error_without_safe_fallback_fails_closed():
    with pytest.raises(AboutGenerationError, match="LLM недоступен"):
        generate_about(Client(error=RuntimeError("offline")), "", None)


def test_prompt_does_not_include_emoji_or_unscoped_portfolio_instruction():
    prompt = build_about_prompt("", None)
    assert "эмодзи" in prompt[0]["content"]
    assert "портфолио" in prompt[0]["content"]


def test_dry_run_marker_is_not_used_for_write_mode():
    assert draft_prefix(True) == "[DRY-RUN]"
    assert draft_prefix(False) == "[INFO] Предложение"


def test_open_about_editor_retries_pre_hydration_noop_click(monkeypatch):
    """The about editor marker must positively confirm a functional click."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 1
    field = MagicMock()
    field.count.return_value = 0
    field.input_value.return_value = ""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    field.wait_for.side_effect = [PlaywrightTimeoutError("not hydrated"), None]

    def click_side_effect():
        if trigger.click.call_count == 2:
            page.url = "https://hh.ru/resume/resume-id"

    trigger.click.side_effect = click_side_effect
    page.locator.side_effect = lambda selector: {
        about_module.resume_page.RESUME_EDIT_ABOUT_BUTTON: trigger,
        about_module.resume_page.RESUME_ABOUT_EDITOR: field,
    }[selector]
    monkeypatch.setattr(about_module, "goto_hh", lambda *_args, **_kwargs: None)

    assert open_about_editor(page, bare_resume("resume-id")) == ""
    assert trigger.click.call_count == 2
    assert field.wait_for.call_count == 2


def test_open_about_editor_waits_for_hidden_but_present_field(monkeypatch):
    """A pre-hydration editor already in the DOM (count==1, not visible) must
    still be clicked and waited on, not returned unread (#339)."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 1
    field = MagicMock()
    # Present in the DOM before hydration — count() == 1, but not yet visible.
    field.count.return_value = 1
    field.input_value.return_value = "Реальный текст, который уже был в поле."
    field.wait_for.return_value = None

    page.locator.side_effect = lambda selector: {
        about_module.resume_page.RESUME_EDIT_ABOUT_BUTTON: trigger,
        about_module.resume_page.RESUME_ABOUT_EDITOR: field,
    }[selector]
    monkeypatch.setattr(about_module, "goto_hh", lambda *_args, **_kwargs: None)

    result = open_about_editor(page, bare_resume("resume-id"))

    assert trigger.click.call_count == 1
    assert field.wait_for.call_count == 1
    assert result == "Реальный текст, который уже был в поле."


def test_open_about_editor_accepts_draft_edit_route(monkeypatch):
    """Draft resumes navigate to the dedicated about editor route (#527)."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 1
    field = MagicMock()
    field.count.return_value = 0
    field.input_value.return_value = ""
    field.wait_for.return_value = None

    def click_side_effect():
        page.url = "https://hh.ru/resume/edit/resume-id/about"

    trigger.click.side_effect = click_side_effect
    page.locator.side_effect = lambda selector: {
        about_module.resume_page.RESUME_EDIT_ABOUT_BUTTON: trigger,
        about_module.resume_page.RESUME_ABOUT_EDITOR: field,
    }[selector]
    monkeypatch.setattr(about_module, "goto_hh", lambda *_args, **_kwargs: None)

    assert open_about_editor(page, bare_resume("resume-id")) == ""


def test_open_about_editor_accepts_draft_edit_route_with_query(monkeypatch):
    """Query parameters on the edit route must not break the route guard (#788 follow-up)."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 1
    field = MagicMock()
    field.count.return_value = 0
    field.input_value.return_value = ""
    field.wait_for.return_value = None

    def click_side_effect():
        page.url = "https://hh.ru/resume/edit/resume-id/about?foo=bar"

    trigger.click.side_effect = click_side_effect
    page.locator.side_effect = lambda selector: {
        about_module.resume_page.RESUME_EDIT_ABOUT_BUTTON: trigger,
        about_module.resume_page.RESUME_ABOUT_EDITOR: field,
    }[selector]
    monkeypatch.setattr(about_module, "goto_hh", lambda *_args, **_kwargs: None)

    assert open_about_editor(page, bare_resume("resume-id")) == ""


def test_open_about_editor_rejects_wrong_route_with_empty_resume_id(monkeypatch):
    """An empty resume_id must not accidentally match a different edit route."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 1
    field = MagicMock()
    field.count.return_value = 0
    field.input_value.return_value = ""
    field.wait_for.return_value = None

    def click_side_effect():
        page.url = "https://hh.ru/resume/edit/other-id/about"

    trigger.click.side_effect = click_side_effect
    page.locator.side_effect = lambda selector: {
        about_module.resume_page.RESUME_EDIT_ABOUT_BUTTON: trigger,
        about_module.resume_page.RESUME_ABOUT_EDITOR: field,
    }[selector]
    monkeypatch.setattr(about_module, "goto_hh", lambda *_args, **_kwargs: None)

    with pytest.raises(AboutGenerationError):
        open_about_editor(page, bare_resume(""))


def test_open_about_editor_still_fails_closed_on_unexpected_route(monkeypatch):
    """The route guard must not be weakened: an unexpected editor route still
    fails closed instead of silently editing the wrong resume (#527)."""
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock()
    trigger.count.return_value = 1
    field = MagicMock()
    field.count.return_value = 0
    field.input_value.return_value = ""
    field.wait_for.return_value = None

    def click_side_effect():
        page.url = "https://hh.ru/resume/edit/resume-id/experience"

    trigger.click.side_effect = click_side_effect
    page.locator.side_effect = lambda selector: {
        about_module.resume_page.RESUME_EDIT_ABOUT_BUTTON: trigger,
        about_module.resume_page.RESUME_ABOUT_EDITOR: field,
    }[selector]
    monkeypatch.setattr(about_module, "goto_hh", lambda *_args, **_kwargs: None)

    with pytest.raises(AboutGenerationError):
        open_about_editor(page, bare_resume("resume-id"))


def test_open_about_editor_fails_fast_when_button_missing(monkeypatch):
    """An empty resume has no about button; the command must fail immediately
    instead of hanging for 2+ minutes on ready_selector timeout (#790)."""
    page = MagicMock(name="Page")
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock(name="trigger")
    trigger.count.return_value = 0
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    trigger.wait_for.side_effect = PlaywrightTimeoutError("not visible")

    page.locator.side_effect = lambda selector: {
        about_module.resume_page.RESUME_EDIT_ABOUT_BUTTON: trigger,
    }[selector]
    monkeypatch.setattr(about_module, "goto_hh", lambda *_args, **_kwargs: None)

    with pytest.raises(AboutGenerationError, match="не появилась после навигации"):
        open_about_editor(page, bare_resume("resume-id"))

    trigger.wait_for.assert_called_once()
    trigger.count.assert_not_called()
    trigger.click.assert_not_called()


def test_open_about_editor_fails_fast_when_button_ambiguous(monkeypatch):
    """Multiple about buttons must also fail fast, not enter the editor flow."""
    page = MagicMock(name="Page")
    page.url = "https://hh.ru/resume/resume-id"
    trigger = MagicMock(name="trigger")
    trigger.count.return_value = 2
    trigger.wait_for.return_value = None

    page.locator.side_effect = lambda selector: {
        about_module.resume_page.RESUME_EDIT_ABOUT_BUTTON: trigger,
    }[selector]
    monkeypatch.setattr(about_module, "goto_hh", lambda *_args, **_kwargs: None)

    with pytest.raises(AboutGenerationError, match="не найдена однозначно"):
        open_about_editor(page, bare_resume("resume-id"))

    trigger.wait_for.assert_called_once()
    trigger.count.assert_called_once()
    trigger.click.assert_not_called()

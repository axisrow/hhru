"""Pure tests for the LLM-assisted inline about-section flow."""

import pytest

from hhru_bot.about import AboutGenerationError, build_about_prompt, generate_about
from hhru_bot.commands.about import draft_prefix

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

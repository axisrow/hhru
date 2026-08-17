"""Focused contract tests for the Responses API transport."""

from types import SimpleNamespace

import pytest

from hhru_bot.ai.responses import ResponsesTransport

pytestmark = pytest.mark.unit


def test_build_kwargs_uses_responses_input_and_tool_shape():
    transport = ResponsesTransport()
    kwargs = transport.build_kwargs(
        "gpt-5-mini",
        [{"role": "system", "content": "be concise"}, {"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}],
        max_tokens=80,
    )
    assert kwargs["input"][0]["role"] == "system"
    assert kwargs["input"][1]["content"] == "hi"
    assert kwargs["max_output_tokens"] == 80
    assert kwargs["tools"] == [
        {"type": "function", "name": "search", "parameters": {"type": "object"}}
    ]
    assert "messages" not in kwargs


def test_normalize_response_text_usage_and_function_call():
    response = SimpleNamespace(
        output_text="draft",
        output=[
            SimpleNamespace(
                type="function_call", call_id="call_1", name="search", arguments='{"q":"x"}'
            )
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14),
    )
    normalized = ResponsesTransport().normalize_response(response)
    assert normalized.content == "draft"
    assert normalized.finish_reason == "tool_calls"
    assert normalized.tool_calls[0].id == "call_1"
    assert normalized.usage.prompt_tokens == 10

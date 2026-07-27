"""Тесты ChatCompletionsTransport: конвертация формата (issue #16, Этап 5).

Без браузера/сети. openai SDK мокается простыми объектами с атрибутами, точно
повторяющими форму ChatCompletion (choices[0].message/tool_calls/usage и т.д.).
Покрываем: convert_messages (sanitization), convert_tools (identity), build_kwargs
(system->developer swap + tools + max_tokens + extra_body), normalize_response
(content/tool_calls/usage/refusal), validate_response.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from hhru_bot.ai.chat_completions import DEVELOPER_ROLE_MODELS, ChatCompletionsTransport
from hhru_bot.ai.types import NormalizedResponse, ToolCall

# --- helpers: mock-объекты в форме OpenAI SDK -------------------------------


def _msg(content=None, tool_calls=None, refusal=None, reasoning=None, reasoning_content=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        refusal=refusal,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
        reasoning_details=None,
        model_extra=None,
    )


def _tool_call_obj(tc_id="call_1", name="search", arguments='{"q":"x"}'):
    return SimpleNamespace(id=tc_id, function=SimpleNamespace(name=name, arguments=arguments))


def _choice(msg, finish_reason="stop"):
    return SimpleNamespace(message=msg, finish_reason=finish_reason)


def _response(msg, finish_reason: Any = "stop", usage=None):
    return SimpleNamespace(choices=[_choice(msg, finish_reason)], usage=usage)


def _usage(prompt=10, completion=5, total=15):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


# --- DEVELOPER_ROLE_MODELS --------------------------------------------------


def test_developer_role_models_constant():
    """Константа портирована дословно из agent/prompt_builder.py:664."""
    assert DEVELOPER_ROLE_MODELS == ("gpt-5", "codex")


# --- convert_messages -------------------------------------------------------


def test_convert_messages_strips_internal_fields():
    """tool_name/timestamp/codex_*/api_content и _-ключи выкидываются."""
    transport = ChatCompletionsTransport()
    messages = [
        {
            "role": "user",
            "content": "hi",
            "tool_name": "x",
            "timestamp": 123,
            "codex_reasoning_items": [],
            "api_content": "y",
            "effect_disposition": "z",
            "_internal_marker": True,
        }
    ]
    out = transport.convert_messages(messages)
    assert out[0] == {"role": "user", "content": "hi"}
    # исходный список не мутируется (возвращается копия при санитизации)
    assert "tool_name" in messages[0]


def test_convert_messages_identity_when_clean():
    """Чистые сообщения возвращаются как есть (та же ссылка)."""
    transport = ChatCompletionsTransport()
    messages = [{"role": "user", "content": "hi"}]
    out = transport.convert_messages(messages)
    assert out is messages


def test_convert_messages_skips_non_dict_entries():
    transport = ChatCompletionsTransport()
    out = transport.convert_messages([{"role": "user", "content": "x"}, None, "junk"])
    assert out[0] == {"role": "user", "content": "x"}


# --- convert_tools ----------------------------------------------------------


def test_convert_tools_identity():
    transport = ChatCompletionsTransport()
    tools = [{"type": "function", "function": {"name": "f"}}]
    assert transport.convert_tools(tools) is tools


# --- build_kwargs -----------------------------------------------------------


def test_build_kwargs_basic():
    transport = ChatCompletionsTransport()
    messages = [{"role": "user", "content": "hi"}]
    kwargs = transport.build_kwargs("gpt-4o", messages)
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["messages"] == messages
    assert "developer" not in [m["role"] for m in kwargs["messages"]]


def test_build_kwargs_system_to_developer_for_gpt5():
    transport = ChatCompletionsTransport()
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    kwargs = transport.build_kwargs("gpt-5-mini", messages)
    assert kwargs["messages"][0]["role"] == "developer"
    # исходный список не мутируется
    assert messages[0]["role"] == "system"


def test_build_kwargs_system_to_developer_for_codex():
    transport = ChatCompletionsTransport()
    messages = [{"role": "system", "content": "x"}]
    kwargs = transport.build_kwargs("codex-1", messages)
    assert kwargs["messages"][0]["role"] == "developer"


def test_build_kwargs_no_swap_for_arbitrary_model():
    transport = ChatCompletionsTransport()
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
    kwargs = transport.build_kwargs("claude-3.5-sonnet", messages)
    assert kwargs["messages"][0]["role"] == "system"


def test_build_kwargs_passes_tools_max_tokens_temperature():
    transport = ChatCompletionsTransport()
    tools = [{"type": "function"}]
    kwargs = transport.build_kwargs(
        "gpt-4o",
        [{"role": "user", "content": "x"}],
        tools,
        max_tokens=128,
        temperature=0.3,
        timeout=12.0,
    )
    assert kwargs["tools"] == tools
    assert kwargs["max_tokens"] == 128
    assert kwargs["temperature"] == 0.3
    assert kwargs["timeout"] == 12.0


def test_build_kwargs_extra_body_and_overrides():
    transport = ChatCompletionsTransport()
    kwargs = transport.build_kwargs(
        "gpt-4o",
        [{"role": "user", "content": "x"}],
        extra_body={"foo": 1},
        request_overrides={"service_tier": "default"},
    )
    assert kwargs["extra_body"] == {"foo": 1}
    assert kwargs["service_tier"] == "default"


# --- normalize_response -----------------------------------------------------


def test_normalize_response_basic_content():
    transport = ChatCompletionsTransport()
    resp = _response(_msg(content="hello"), usage=_usage())
    nr = transport.normalize_response(resp)
    assert isinstance(nr, NormalizedResponse)
    assert nr.content == "hello"
    assert nr.finish_reason == "stop"
    assert nr.tool_calls is None
    assert nr.usage is not None
    assert nr.usage.prompt_tokens == 10
    assert nr.usage.completion_tokens == 5


def test_normalize_response_finish_reason_none_defaults_to_stop():
    transport = ChatCompletionsTransport()
    resp = _response(_msg(content="x"), finish_reason=None)
    assert transport.normalize_response(resp).finish_reason == "stop"


def test_normalize_response_integer_finish_reason_coerced():
    """Некоторые шлюзы возвращают числовой finish_reason."""
    transport = ChatCompletionsTransport()
    resp = _response(_msg(content="x"), finish_reason=24)
    assert transport.normalize_response(resp).finish_reason == "24"


def test_normalize_response_tool_calls():
    transport = ChatCompletionsTransport()
    resp = _response(_msg(content=None, tool_calls=[_tool_call_obj()]))
    nr = transport.normalize_response(resp)
    assert nr.tool_calls is not None
    tc: ToolCall = nr.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "search"
    assert tc.arguments == '{"q":"x"}'
    # tc.function.* работает через back-compat свойство
    assert tc.function.name == "search"


def test_normalize_response_refusal_promoted_to_content_when_empty():
    transport = ChatCompletionsTransport()
    resp = _response(_msg(content=None, refusal="I can't help with that"))
    nr = transport.normalize_response(resp)
    assert nr.content == "I can't help with that"
    assert nr.finish_reason == "content_filter"
    assert nr.provider_data["refusal"] == "I can't help with that"


def test_normalize_response_refusal_keeps_real_content():
    """Если рядом с refusal есть реальный content — не повышаем до content_filter."""
    transport = ChatCompletionsTransport()
    resp = _response(_msg(content="real answer", refusal="minor note"), finish_reason="stop")
    nr = transport.normalize_response(resp)
    assert nr.content == "real answer"
    assert nr.finish_reason == "stop"


def test_normalize_response_reasoning_content_into_provider_data():
    transport = ChatCompletionsTransport()
    resp = _response(_msg(content="x", reasoning_content="thoughts..."))
    nr = transport.normalize_response(resp)
    assert nr.provider_data["reasoning_content"] == "thoughts..."


# --- validate_response / extract_cache_stats --------------------------------


def test_validate_response_truthy_for_valid():
    transport = ChatCompletionsTransport()
    assert transport.validate_response(_response(_msg(content="x"))) is True


def test_validate_response_falsey_for_empty_or_none():
    transport = ChatCompletionsTransport()
    assert transport.validate_response(None) is False
    assert transport.validate_response(SimpleNamespace(choices=None)) is False
    assert transport.validate_response(SimpleNamespace(choices=[])) is False


def test_extract_cache_stats_from_prompt_tokens_details():
    transport = ChatCompletionsTransport()
    details = SimpleNamespace(cached_tokens=42, cache_write_tokens=7)
    resp = SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens_details=details),
    )
    stats = transport.extract_cache_stats(resp)
    assert stats == {"cached_tokens": 42, "creation_tokens": 7}


def test_extract_cache_stats_none_when_absent():
    transport = ChatCompletionsTransport()
    resp = _response(_msg(content="x"), usage=_usage())  # нет prompt_tokens_details
    assert transport.extract_cache_stats(resp) is None

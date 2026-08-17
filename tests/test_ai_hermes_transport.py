"""LLMClient contract over the full Hermes resolver chain (issue #230)."""

from __future__ import annotations

import logging
import sys
import types
from types import SimpleNamespace

import pytest

from hhru_bot.ai.llm_client import LLMClient, _normalize_response
from hhru_bot.config_sections.ai import AiConfig

pytestmark = pytest.mark.unit


def _cfg():
    return AiConfig(provider="openai", model="gpt-5.5", base_url="https://api.example.com/v1")


def _install_fake_call_llm(monkeypatch, response, capture):
    """Install a resolver fake that records the public ``call_llm`` contract."""
    mod = types.ModuleType("agent.auxiliary_client")

    def call_llm(**kwargs):
        capture.append(kwargs)
        kwargs["route_info"].update(provider="nous", model="nous-fast")
        return response

    mod.call_llm = call_llm
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", mod)


def _response(content="hello", finish_reason="stop", usage=None, tool_calls=None, refusal=None):
    msg = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        refusal=refusal,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
        model_extra=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)],
        usage=usage,
    )


def _usage(prompt=10, completion=5, total=15):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


def test_chat_uses_full_resolver_chain_and_logs_selected_route(monkeypatch, caplog):
    capture = []
    _install_fake_call_llm(monkeypatch, _response(content="письмо", usage=_usage()), capture)
    client = LLMClient(_cfg())

    with caplog.at_level(logging.INFO, logger="hhru_bot.ai.llm_client"):
        result = client.chat(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            temperature=0.7,
            max_tokens=80,
            timeout=12.0,
        )

    assert len(capture) == 1
    kwargs = capture[0]
    assert kwargs["task"] == "hhru"
    assert kwargs["messages"][1]["content"] == "u"
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 80
    assert kwargs["timeout"] == 12.0
    # No explicit provider/base_url/api_key bypasses the Hermes resolver.
    assert "provider" not in kwargs and "base_url" not in kwargs and "api_key" not in kwargs
    assert "main_runtime" not in kwargs
    assert result.provider_data["hermes_route"] == {"provider": "nous", "model": "nous-fast"}
    assert "provider=nous model=nous-fast" in caplog.text


def test_call_never_overrides_existing_hermes_config(monkeypatch):
    capture = []
    _install_fake_call_llm(monkeypatch, _response(), capture)

    LLMClient(_cfg()).chat([{"role": "user", "content": "u"}])

    assert "main_runtime" not in capture[0]
    assert "provider" not in capture[0]
    assert "api_key" not in capture[0]


def test_init_import_error_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)
    monkeypatch.setitem(sys.modules, "agent", None)
    with pytest.raises(ImportError, match=r"pip install -e '\.\[ai\]'"):
        LLMClient(_cfg())


def test_chat_passes_tools_and_extra_body(monkeypatch):
    capture = []
    _install_fake_call_llm(monkeypatch, _response(), capture)
    tools = [{"type": "function", "function": {"name": "f"}}]

    LLMClient(_cfg()).chat(
        [{"role": "user", "content": "x"}], tools=tools, extra_body={"foo": "bar"}
    )

    assert capture[0]["tools"] is tools
    assert capture[0]["extra_body"] == {"foo": "bar"}


def test_chat_omits_none_params(monkeypatch):
    # None = «не задано»: не пересылаем его в Hermes (как прежний transport),
    # чтобы не переопределить дефолт SDK значением None.
    capture = []
    _install_fake_call_llm(monkeypatch, _response(), capture)

    LLMClient(_cfg()).chat([{"role": "user", "content": "x"}], timeout=None, temperature=None)

    assert "timeout" not in capture[0]
    assert "temperature" not in capture[0]


def test_chat_propagates_resolver_exception(monkeypatch):
    class _Boom(Exception):
        pass

    mod = types.ModuleType("agent.auxiliary_client")
    mod.call_llm = lambda **kwargs: (_ for _ in ()).throw(_Boom("connection error"))
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", mod)

    with pytest.raises(_Boom):
        LLMClient(_cfg()).chat([{"role": "user", "content": "x"}])


def test_normalize_finish_reason_none_defaults_to_stop():
    assert _normalize_response(_response(content="x", finish_reason=None)).finish_reason == "stop"


def test_normalize_integer_finish_reason_coerced():
    assert _normalize_response(_response(content="x", finish_reason=24)).finish_reason == "24"


def test_normalize_tool_calls():
    tc = SimpleNamespace(
        id="call_1", function=SimpleNamespace(name="search", arguments='{"q":"x"}')
    )
    nr = _normalize_response(_response(content=None, tool_calls=[tc]))
    assert nr.tool_calls[0].id == "call_1"
    assert nr.tool_calls[0].name == "search"
    assert nr.tool_calls[0].arguments == '{"q":"x"}'


def test_normalize_refusal_promoted_when_sole_payload():
    nr = _normalize_response(_response(content=None, refusal="не могу помочь"))
    assert nr.content == "не могу помочь"
    assert nr.finish_reason == "content_filter"
    assert nr.provider_data["refusal"] == "не могу помочь"


def test_normalize_reasoning_content_and_route_metadata():
    resp = _response(content="x")
    resp.choices[0].message.reasoning_content = "мысли..."
    nr = _normalize_response(resp, route_info={"provider": "openrouter", "model": "x"})
    assert nr.provider_data["reasoning_content"] == "мысли..."
    assert nr.provider_data["hermes_route"] == {"provider": "openrouter", "model": "x"}


def test_normalize_content_only_message_without_tool_calls_attr():
    # Hermes recovery может вернуть message только с content — без атрибута
    # tool_calls вовсе. Нормализация не должна падать AttributeError.
    msg = SimpleNamespace(
        content="x",
        refusal=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
        model_extra=None,
    )
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)
    nr = _normalize_response(resp)
    assert nr.content == "x"
    assert nr.tool_calls is None
    assert nr.finish_reason == "stop"

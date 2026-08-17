"""Тесты LLMClient на Responses API (issue #230).

Без сети и без установленного openai: ленивый импорт ``openai.OpenAI``
подменяется фейковым модулем в sys.modules, ответы — SimpleNamespace в
форме SDK-объекта ``Response`` (output[]/status/incomplete_details/usage).
Проверяем: маппинг kwargs (instructions/input/max_output_tokens/store),
нормализацию (finish_reason-ветки, refusal, usage, function_call),
status=failed → исключение, пустой ключ не читает env, ImportError с
подсказкой установки.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from hhru_bot.ai.llm_client import LLMClient, _normalize_response
from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.config_sections.ai import AiConfig

pytestmark = pytest.mark.unit


def _cfg():
    return AiConfig(provider="openai", model="gpt-5.5", base_url="https://api.example.com/v1")


def _install_fake_openai(monkeypatch, response):
    """Подменяет openai.OpenAI фейком; возвращает список созданных клиентов."""
    created = []

    class _OpenAI:
        def __init__(self, *, base_url=None, api_key=None):
            self.base_url = base_url
            self.api_key = api_key
            self.calls = []
            self.responses = SimpleNamespace(create=self._create)
            created.append(self)

        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return response

    mod = types.ModuleType("openai")
    mod.OpenAI = _OpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)
    return created


def _resp(output=None, status="completed", usage=None, incomplete=None, error=None):
    return SimpleNamespace(
        output=output or [],
        status=status,
        usage=usage,
        incomplete_details=incomplete,
        error=error,
    )


def _text_item(*texts, refusal=None):
    blocks = [SimpleNamespace(type="output_text", text=t) for t in texts]
    if refusal is not None:
        blocks.append(SimpleNamespace(type="refusal", refusal=refusal))
    return SimpleNamespace(type="message", content=blocks)


def _usage(inp=10, outp=5, total=15):
    return SimpleNamespace(input_tokens=inp, output_tokens=outp, total_tokens=total)


# --- построение клиента ------------------------------------------------------


def test_init_passes_base_url_and_env_key(monkeypatch):
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk-env")
    created = _install_fake_openai(monkeypatch, _resp(output=[_text_item("ok")]))
    LLMClient(_cfg())
    assert created[0].base_url == "https://api.example.com/v1"
    assert created[0].api_key == "sk-env"


def test_init_empty_key_passed_as_empty_string(monkeypatch):
    """Пустой ключ → api_key="" (не None): SDK при None полез бы в env
    OPENAI_API_KEY — подсос чужих кредов пресечён; 401 ждёт на запросе."""
    monkeypatch.delenv("HHRU_AI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-foreign-must-not-be-used")
    created = _install_fake_openai(monkeypatch, _resp(output=[_text_item("ok")]))
    LLMClient(_cfg())
    assert created[0].api_key == ""


def test_init_import_error_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.delenv("HHRU_AI_API_KEY", raising=False)
    with pytest.raises(ImportError, match=r"pip install -e '\.\[ai\]'"):
        LLMClient(_cfg())


# --- chat(): маппинг kwargs --------------------------------------------------


def test_chat_maps_messages_to_instructions_and_input(monkeypatch):
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    created = _install_fake_openai(monkeypatch, _resp(output=[_text_item("ok")]))
    client = LLMClient(_cfg())
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    nr = client.chat(messages, temperature=0.7, timeout=12.0, max_tokens=256)
    kwargs = created[0].calls[0]
    assert kwargs["model"] == "gpt-5.5"
    assert kwargs["instructions"] == "be brief"
    assert kwargs["input"] == [{"role": "user", "content": "hi"}]
    assert kwargs["store"] is False
    assert kwargs["temperature"] == 0.7
    assert kwargs["timeout"] == 12.0
    assert kwargs["max_output_tokens"] == 256
    assert "max_tokens" not in kwargs
    assert isinstance(nr, NormalizedResponse) and nr.content == "ok"


def test_chat_without_system_omits_instructions(monkeypatch):
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    created = _install_fake_openai(monkeypatch, _resp(output=[_text_item("x")]))
    client = LLMClient(_cfg())
    client.chat([{"role": "user", "content": "u"}])
    kwargs = created[0].calls[0]
    assert "instructions" not in kwargs
    assert kwargs["input"] == [{"role": "user", "content": "u"}]


def test_chat_joins_multiple_system_messages(monkeypatch):
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    created = _install_fake_openai(monkeypatch, _resp(output=[_text_item("x")]))
    client = LLMClient(_cfg())
    client.chat(
        [
            {"role": "system", "content": "first"},
            {"role": "user", "content": "u"},
            {"role": "system", "content": "second"},
        ]
    )
    assert created[0].calls[0]["instructions"] == "first\nsecond"


def test_chat_converts_tools_to_responses_format(monkeypatch):
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    created = _install_fake_openai(monkeypatch, _resp(output=[_text_item("x")]))
    client = LLMClient(_cfg())
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    client.chat([{"role": "user", "content": "u"}], tools=tools)
    assert created[0].calls[0]["tools"] == [
        {"type": "function", "name": "f", "parameters": {"type": "object"}}
    ]


# --- нормализация ------------------------------------------------------------


def test_normalize_aggregates_multiple_output_text_blocks():
    resp = _resp(output=[_text_item("часть "), _text_item("вторая")], usage=_usage())
    nr = _normalize_response(resp)
    assert nr.content == "часть вторая"
    assert nr.finish_reason == "stop"
    assert nr.usage.prompt_tokens == 10
    assert nr.usage.completion_tokens == 5
    assert nr.usage.total_tokens == 15


def test_normalize_empty_output_content_is_none():
    nr = _normalize_response(_resp(output=[]))
    assert nr.content is None
    assert nr.finish_reason == "stop"


def test_normalize_incomplete_max_output_tokens():
    resp = _resp(
        output=[_text_item("обрыв")],
        status="incomplete",
        incomplete=SimpleNamespace(reason="max_output_tokens"),
    )
    assert _normalize_response(resp).finish_reason == "length"


def test_normalize_incomplete_content_filter():
    resp = _resp(
        output=[_text_item("x")],
        status="incomplete",
        incomplete=SimpleNamespace(reason="content_filter"),
    )
    assert _normalize_response(resp).finish_reason == "content_filter"


def test_normalize_failed_status_raises():
    """status=failed — ошибка протокола: исключение, а не пустой ответ
    (устоявшийся путь отказа → fallback потребителя)."""
    resp = _resp(status="failed", error="model_error")
    with pytest.raises(RuntimeError, match="status=failed"):
        _normalize_response(resp)


def test_normalize_refusal_promoted_when_sole_payload():
    resp = _resp(output=[_text_item(refusal="не могу помочь")])
    nr = _normalize_response(resp)
    assert nr.content == "не могу помочь"
    assert nr.finish_reason == "content_filter"
    assert nr.provider_data["refusal"] == "не могу помочь"


def test_normalize_refusal_keeps_real_content():
    resp = _resp(output=[_text_item("реальный ответ", refusal="примечание")])
    nr = _normalize_response(resp)
    assert nr.content == "реальный ответ"
    assert nr.finish_reason == "stop"


def test_normalize_function_call_item():
    item = SimpleNamespace(
        type="function_call", call_id="call_1", name="search", arguments='{"q":"x"}'
    )
    nr = _normalize_response(_resp(output=[item]))
    assert nr.tool_calls[0].id == "call_1"
    assert nr.tool_calls[0].name == "search"
    assert nr.tool_calls[0].arguments == '{"q":"x"}'


def test_chat_sdk_exception_propagates_unchanged(monkeypatch):
    """Слой не ретраит и не переключает провайдера: исключение create()
    доходит до потребителя как есть — fallback остаётся на letters/scoring."""

    class _Boom(Exception):
        pass

    class _FailingCreate:
        def __call__(self, **kwargs):
            raise _Boom("connection error")

    class _OpenAI:
        def __init__(self, *, base_url=None, api_key=None):
            self.responses = SimpleNamespace(create=_FailingCreate())

    mod = types.ModuleType("openai")
    mod.OpenAI = _OpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    client = LLMClient(_cfg())
    with pytest.raises(_Boom):
        client.chat([{"role": "user", "content": "x"}])

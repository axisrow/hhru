"""Тесты LLMClient поверх hermes-agent-axisrow (issue #230).

Без сети и без установленного hermes: ленивый импорт
``agent.auxiliary_client.resolve_provider_client`` подменяется фейковым
модулем в sys.modules, SDK-клиент — простыми объектами в форме
``CodexAuxiliaryClient`` (``.chat.completions.create()`` → chat-подобный
namespace). Проверяем контракт обёртки: параметры резолва (custom + явные
креды + codex_responses — контроль расходов, фаза 1 #230), kwargs create(),
нормализация ответа (логика экс-порта chat_completions), ImportError с
подсказкой установки, плейсхолдер пустого ключа.
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

PLACEHOLDER = "no-key-required"


def _cfg():
    return AiConfig(provider="openai", model="gpt-5.5", base_url="https://api.example.com/v1")


class _FakeCreate:
    """Записывает kwargs и возвращает заранее заданный ответ."""

    def __init__(self, response):
        self.kwargs = None
        self._response = response

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response):
        create = _FakeCreate(response)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        self.create = create


def _install_fake_resolver(monkeypatch, client, model="gpt-5.5", capture=None):
    """Подменяет agent.auxiliary_client фейком с capture-списком вызовов."""
    mod = types.ModuleType("agent.auxiliary_client")

    def resolve_provider_client(provider, **kwargs):
        if capture is not None:
            capture.append((provider, kwargs))
        return client, model

    mod.resolve_provider_client = resolve_provider_client
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


# --- резолв клиента ----------------------------------------------------------


def test_init_resolves_custom_with_explicit_creds_and_responses_mode(monkeypatch):
    """Фаза 1 #230: только custom + явные base_url/key/model + codex_responses.

    Никакого auto-chain и borrowed-кредов hermes — контроль расходов.
    """
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk-env")
    capture = []
    _install_fake_resolver(monkeypatch, _FakeClient(_response()), capture=capture)
    LLMClient(_cfg())
    assert len(capture) == 1
    provider, kwargs = capture[0]
    assert provider == "custom"
    assert kwargs["explicit_base_url"] == "https://api.example.com/v1"
    assert kwargs["explicit_api_key"] == "sk-env"
    assert kwargs["model"] == "gpt-5.5"
    assert kwargs["api_mode"] == "codex_responses"


def test_init_empty_key_uses_placeholder(monkeypatch):
    """Пустой ключ → no-key-required: локальные серверы работают, удалённые
    падают 401 на запросе; подсос OPENAI_API_KEY/~.hermes пресечён."""
    monkeypatch.delenv("HHRU_AI_API_KEY", raising=False)
    capture = []
    _install_fake_resolver(monkeypatch, _FakeClient(_response()), capture=capture)
    LLMClient(_cfg())
    assert capture[0][1]["explicit_api_key"] == PLACEHOLDER


def test_init_import_error_hint(monkeypatch):
    """Без hermes — ImportError с подсказкой установки (контракт _common.py).

    sys.modules[...] = None блокирует импорт даже когда пакет реально
    установлен в окружении: None в sys.modules → ImportError у import-машины.
    """
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.delenv("HHRU_AI_API_KEY", raising=False)
    with pytest.raises(ImportError, match=r"pip install -e '\.\[ai\]'"):
        LLMClient(_cfg())


def test_init_none_client_raises_runtime_error(monkeypatch):
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    _install_fake_resolver(monkeypatch, None, model=None)
    with pytest.raises(RuntimeError, match="api.example.com"):
        LLMClient(_cfg())


# --- chat() ------------------------------------------------------------------


def test_chat_forwards_model_messages_and_params(monkeypatch):
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    fake = _FakeClient(_response(content="письмо", usage=_usage()))
    _install_fake_resolver(monkeypatch, fake)
    client = LLMClient(_cfg())
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    nr = client.chat(messages, temperature=0.7, timeout=12.0)
    assert fake.create.kwargs["model"] == "gpt-5.5"
    assert fake.create.kwargs["messages"] is messages
    assert fake.create.kwargs["temperature"] == 0.7
    assert fake.create.kwargs["timeout"] == 12.0
    assert isinstance(nr, NormalizedResponse)
    assert nr.content == "письмо"
    assert nr.finish_reason == "stop"
    assert nr.usage is not None and nr.usage.total_tokens == 15


def test_chat_passes_tools_when_present(monkeypatch):
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    fake = _FakeClient(_response())
    _install_fake_resolver(monkeypatch, fake)
    client = LLMClient(_cfg())
    tools = [{"type": "function", "function": {"name": "f"}}]
    client.chat([{"role": "user", "content": "x"}], tools=tools)
    assert fake.create.kwargs["tools"] is tools


def test_chat_sdk_exception_propagates_unchanged(monkeypatch):
    """Слой не ретраит и не переключает провайдера (call_llm-патология фазы 1):
    исключение create() доходит до потребителя как есть — fallback на шаблон
    остаётся ответственностью letters/scoring."""

    class _Boom(Exception):
        pass

    class _FailingCreate:
        def __call__(self, **kwargs):
            raise _Boom("connection error")

    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_FailingCreate()))
    )
    monkeypatch.setenv("HHRU_AI_API_KEY", "sk")
    _install_fake_resolver(monkeypatch, fake)
    client = LLMClient(_cfg())
    with pytest.raises(_Boom):
        client.chat([{"role": "user", "content": "x"}])


# --- _normalize_response (логика экс-порта chat_completions #16) -------------


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
    assert nr.tool_calls[0].function.name == "search"


def test_normalize_refusal_promoted_when_sole_payload():
    nr = _normalize_response(_response(content=None, refusal="не могу помочь"))
    assert nr.content == "не могу помочь"
    assert nr.finish_reason == "content_filter"
    assert nr.provider_data["refusal"] == "не могу помочь"


def test_normalize_refusal_keeps_real_content():
    nr = _normalize_response(_response(content="реальный ответ", refusal="примечание"))
    assert nr.content == "реальный ответ"
    assert nr.finish_reason == "stop"


def test_normalize_reasoning_content_into_provider_data():
    resp = _response(content="x")
    resp.choices[0].message.reasoning_content = "мысли..."
    nr = _normalize_response(resp)
    assert nr.provider_data["reasoning_content"] == "мысли..."

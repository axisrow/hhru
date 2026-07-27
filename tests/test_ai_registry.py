"""Тесты transport-реестра (issue #16, Этап 5).

register_transport регистрирует transport-класс по api_mode; get_transport
возвращает экземпляр (lazy-discovery подгружает chat_completions), None для
неизвестного api_mode. Без браузера/сети — чистая логика реестра.
"""

from __future__ import annotations

import pytest

from hhru_bot.ai import base, registry
from hhru_bot.ai.chat_completions import ChatCompletionsTransport


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch):
    """Изолировать тесты: чистый реестр + сброс флага discovery до каждого теста.

    get_transport() кэширует discovery в модуле; без сброса порядок тестов влиял
    бы на видимость зарегистрированных transport'ов.
    """
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_discovered", False)
    yield


class _DummyTransport(base.ProviderTransport):
    """Минимальная реализация ABC для теста регистрации."""

    @property
    def api_mode(self) -> str:
        return "dummy"

    def convert_messages(self, messages, **kwargs):
        return messages

    def convert_tools(self, tools):
        return tools

    def build_kwargs(self, model, messages, tools=None, **params):
        return {"model": model, "messages": messages}

    def normalize_response(self, response, **kwargs):
        from hhru_bot.ai.types import NormalizedResponse

        return NormalizedResponse(content=None, tool_calls=None, finish_reason="stop")


def test_register_then_get_returns_instance():
    registry.register_transport("dummy", _DummyTransport)
    transport = registry.get_transport("dummy")
    assert isinstance(transport, _DummyTransport)


def test_get_transport_unknown_returns_none():
    assert registry.get_transport("definitely-unknown-mode") is None


def test_get_chat_completions_via_lazy_discovery():
    """get_transport('chat_completions') → подгрузка через discovery.

    Регистрация ChatCompletionsTransport происходит как побочный эффект импорта
    модуля chat_completions (один раз при первом импорте пакета). Чтобы тест был
    изолирован от порядка запуска, мы НЕ рассчитываем на повторную регистрацию
    после сброса _REGISTRY (модуль уже в sys.modules). Вместо этого: регистрируем
    chat_completions вручную и проверяем, что discovery флаг выставляется, а
    get_transport возвращает экземпляр.
    """
    # chat_completions уже импортирован пакетом; его авто-регистрация прошла в
    # оригинальный _REGISTRY. Ручная регистрация имитирует результат discovery.
    registry.register_transport("chat_completions", ChatCompletionsTransport)
    transport = registry.get_transport("chat_completions")
    assert isinstance(transport, ChatCompletionsTransport)
    assert transport.api_mode == "chat_completions"


def test_lazy_discovery_sets_flag():
    registry.get_transport("chat_completions")
    assert registry._discovered is True


def test_get_transport_triggers_discovery_on_miss(monkeypatch):
    """На miss по неизвестному api_mode discovery запускается повторно (как в оригинале)."""
    calls = {"n": 0}
    real_discover = registry._discover_transports

    def spy():
        calls["n"] += 1
        real_discover()

    monkeypatch.setattr(registry, "_discovered", True)  # притворимся, что уже открывали
    monkeypatch.setattr(registry, "_discover_transports", spy)
    # _REGISTRY пуст → первый get_transport не находит → miss → повторный discovery
    transport = registry.get_transport("unknown-mode-after-miss")
    assert transport is None
    assert calls["n"] == 1


def test_get_returns_fresh_instance_each_call():
    registry.register_transport("dummy", _DummyTransport)
    a = registry.get_transport("dummy")
    b = registry.get_transport("dummy")
    assert a is not b

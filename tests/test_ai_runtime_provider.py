"""Тесты resolve_runtime_provider (issue #16, Этап 5).

Тонкий порт: по AiConfig (provider/model/base_url) + env-ключу строит runtime
dict {provider, api_mode, base_url, api_key, model, source}. Без сети и без
hermes-зависимостей — чистая функция, тестируется на моках env.

Приоритет ключа: явный аргумент > env HHRU_AI_API_KEY > пустая строка.
"""

from __future__ import annotations

import pytest

from hhru_bot.ai.runtime_provider import (
    API_KEY_ENV_VAR,
    DEFAULT_API_MODE,
    resolve_runtime_provider,
)
from hhru_bot.config_sections.ai import AiConfig

pytestmark = pytest.mark.unit


def _cfg():
    return AiConfig(provider="openai", model="gpt-4o", base_url="https://api.openai.com/v1")


def test_returns_full_runtime_dict(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV_VAR, "sk-from-env")
    runtime = resolve_runtime_provider(_cfg())
    assert runtime == {
        "provider": "openai",
        "api_mode": DEFAULT_API_MODE,
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-from-env",
        "model": "gpt-4o",
        "source": "config",
    }


def test_explicit_api_key_wins_over_env(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV_VAR, "sk-from-env")
    runtime = resolve_runtime_provider(_cfg(), api_key="sk-explicit")
    assert runtime["api_key"] == "sk-explicit"
    assert runtime["source"] == "explicit"


def test_env_key_used_when_no_explicit(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV_VAR, "sk-env")
    runtime = resolve_runtime_provider(_cfg())
    assert runtime["api_key"] == "sk-env"
    assert runtime["source"] == "config"


def test_empty_api_key_when_neither_arg_nor_env(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    runtime = resolve_runtime_provider(_cfg())
    # Не падает — пустой ключ; реальный SDK сообщит об ошибке при вызове.
    assert runtime["api_key"] == ""
    assert runtime["source"] == "config"


def test_api_mode_override(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    runtime = resolve_runtime_provider(_cfg(), api_key="k", api_mode="anthropic_messages")
    assert runtime["api_mode"] == "anthropic_messages"


def test_explicit_empty_string_key_falls_back_to_env(monkeypatch):
    """Пустая/пробельная строка как явный ключ — считается «не задано», идём в env."""
    monkeypatch.setenv(API_KEY_ENV_VAR, "sk-env")
    runtime = resolve_runtime_provider(_cfg(), api_key="   ")
    assert runtime["api_key"] == "sk-env"
    assert runtime["source"] == "config"

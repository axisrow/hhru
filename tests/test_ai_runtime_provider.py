"""Tests for AI diagnostic runtime metadata (issue #230)."""

from __future__ import annotations

import pytest

from hhru_bot.ai.runtime_provider import DEFAULT_API_MODE, resolve_runtime_provider
from hhru_bot.config_sections.ai import AiConfig

pytestmark = pytest.mark.unit


def _cfg():
    return AiConfig(provider="openai", model="gpt-4o", base_url="https://api.openai.com/v1")


def test_returns_metadata_without_credentials():
    runtime = resolve_runtime_provider(_cfg())
    assert runtime == {
        "provider": "openai",
        "api_mode": DEFAULT_API_MODE,
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
    }
    assert "api_key" not in runtime


def test_api_mode_override():
    runtime = resolve_runtime_provider(_cfg(), api_mode="anthropic_messages")
    assert runtime["api_mode"] == "anthropic_messages"

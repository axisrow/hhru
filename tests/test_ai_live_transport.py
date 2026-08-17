"""Live-smoke AI-транспорта на Responses API (issue #230).

Реальный LLM-вызов за деньги пользователя (категория live_read по #230:
мутаций нигде нет, единственный побочный эффект — токены провайдера).
По умолчанию исключён из прогона (pytest -m live_read); без полного набора
env-переменных тихо скипается:

    HHRU_AI_LIVE_BASE_URL  -- например https://api.openai.com/v1
    HHRU_AI_LIVE_MODEL     -- например gpt-5.5
    HHRU_AI_API_KEY        -- ключ провайдера

Запуск:
    HHRU_AI_LIVE_BASE_URL=... HHRU_AI_LIVE_MODEL=... HHRU_AI_API_KEY=... \
        pytest -m live_read tests/test_ai_live_transport.py

Проверяет контракт end-to-end: один вызов Responses API на указанный
base_url (endpoint обязан поддерживать /responses) и ненулевой текст
в ответе.
"""

from __future__ import annotations

import os

import pytest

from hhru_bot.ai.llm_client import LLMClient
from hhru_bot.config_sections.ai import AiConfig

_BASE = os.environ.get("HHRU_AI_LIVE_BASE_URL", "")
_MODEL = os.environ.get("HHRU_AI_LIVE_MODEL", "")
_KEY = os.environ.get("HHRU_AI_API_KEY", "")

pytestmark = [
    pytest.mark.live_read,
    pytest.mark.skipif(
        not (_BASE and _MODEL and _KEY),
        reason="нужны HHRU_AI_LIVE_BASE_URL, HHRU_AI_LIVE_MODEL и HHRU_AI_API_KEY",
    ),
]


def test_live_single_call_returns_text():
    cfg = AiConfig(provider="live-smoke", model=_MODEL, base_url=_BASE)
    client = LLMClient(cfg, api_key=_KEY)
    assert client.runtime["base_url"] == _BASE
    nr = client.chat(
        [{"role": "user", "content": "Ответь ровно одним словом: ping"}],
        timeout=60.0,
    )
    assert nr.content and nr.content.strip(), f"пустой ответ (finish_reason={nr.finish_reason})"

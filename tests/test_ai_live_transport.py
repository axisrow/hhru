"""Live-smoke AI-транспорта поверх hermes-agent-axisrow (issue #230).

Реальный LLM-вызов за деньги пользователя (категория live_read по #230:
мутаций нигде нет, единственный побочный эффект — токены провайдера).
По умолчанию исключён из прогона (pytest -m live_read); без явного opt-in
тихо скипается:

    HHRU_AI_LIVE=1

Запуск:
    HHRU_AI_LIVE=1 pytest -m live_read tests/test_ai_live_transport.py

Проверяет один полный Hermes resolver-chain вызов с существующими Hermes
credentials/config и ненулевой текст в ответе. Ключи и endpoint hhru не
читает и не передаёт.
"""

from __future__ import annotations

import os

import pytest

from hhru_bot.ai.llm_client import LLMClient
from hhru_bot.config_sections.ai import AiConfig

_LIVE = os.environ.get("HHRU_AI_LIVE", "") == "1"

pytestmark = [
    pytest.mark.live_read,
    pytest.mark.skipif(
        not _LIVE,
        reason="требуется явный opt-in HHRU_AI_LIVE=1 и настроенный Hermes",
    ),
]


def test_live_single_call_returns_text():
    cfg = AiConfig(provider="hermes", model="configured", base_url="configured-by-hermes")
    client = LLMClient(cfg)
    nr = client.chat(
        [{"role": "user", "content": "Ответь ровно одним словом: ping"}],
        timeout=60.0,
    )
    assert nr.content and nr.content.strip(), f"пустой ответ (finish_reason={nr.finish_reason})"

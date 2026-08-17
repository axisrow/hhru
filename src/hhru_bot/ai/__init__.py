"""AI transport layer for hhru (issue #230).

Обёртка над pip-зависимостью ``hermes-agent-axisrow`` (Responses API) —
заменяет самодельный замороженный порт из NousResearch/hermes-agent
(#16/#96). ``hermes-agent-axisrow`` — опциональная зависимость (группа
``[ai]``); этот пакет импортируется без неё.

Public surface:
    LLMClient              -- синхронный клиент на Responses API (lazy import)
    NormalizedResponse     -- канонический нормализованный ответ API
    ToolCall / Usage       -- строительные блоки ответа
    resolve_runtime_provider -- runtime-запись из AiConfig + env-ключ
"""

from __future__ import annotations

from .llm_client import LLMClient
from .runtime_provider import resolve_runtime_provider
from .types import NormalizedResponse, ToolCall, Usage, build_tool_call, map_finish_reason

__all__ = [
    "LLMClient",
    "NormalizedResponse",
    "ToolCall",
    "Usage",
    "build_tool_call",
    "map_finish_reason",
    "resolve_runtime_provider",
]

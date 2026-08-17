"""AI transport layer for hhru (issue #16, Stage 5).

Thin LLM client + provider transport abstraction ported from
NousResearch/hermes-agent (MIT). ``openai`` is an optional dependency
(install group ``[ai]``); this package imports without it.

Public surface:
    LLMClient              -- synchronous OpenAI-compat client (lazy openai import)
    NormalizedResponse     -- canonical normalized API response
    ToolCall / Usage       -- response building blocks
    get_transport          -- resolve a transport instance by api_mode
    register_transport     -- register a transport class for an api_mode
    resolve_runtime_provider -- build a runtime entry from AiConfig + env key
    ChatCompletionsTransport -- legacy OpenAI-compat transport
    ResponsesTransport       -- the default OpenAI Responses API transport
    ProviderTransport      -- ABC for provider transports
"""

from __future__ import annotations

from .base import ProviderTransport
from .chat_completions import ChatCompletionsTransport
from .llm_client import LLMClient
from .registry import get_transport, register_transport
from .responses import ResponsesTransport
from .runtime_provider import resolve_runtime_provider
from .types import NormalizedResponse, ToolCall, Usage, build_tool_call, map_finish_reason

__all__ = [
    "LLMClient",
    "NormalizedResponse",
    "ToolCall",
    "Usage",
    "build_tool_call",
    "map_finish_reason",
    "get_transport",
    "register_transport",
    "resolve_runtime_provider",
    "ChatCompletionsTransport",
    "ResponsesTransport",
    "ProviderTransport",
]

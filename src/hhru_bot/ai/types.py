# Ported from NousResearch/hermes-agent (MIT).
# Copyright (c) 2025 Nous Research.
#
# Shared types for normalized provider responses.
#
# These dataclasses define the canonical shape that all provider adapters
# normalize responses to.  The shared surface is intentionally minimal -- only
# fields that every downstream consumer reads are top-level. Protocol-specific
# state goes in ``provider_data`` dicts (response-level and per-tool-call) so
# that protocol-aware code paths can access it without polluting the shared type.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A normalized tool call from any provider.

    ``id`` is the protocol's canonical identifier -- what gets used in
    ``tool_call_id`` / ``tool_use_id`` when constructing tool result messages.
    May be ``None`` when the provider omits it.

    ``provider_data`` carries per-tool-call protocol metadata that only
    protocol-aware code reads (e.g. Gemini ``extra_content`` / thought_signature).
    """

    id: str | None
    name: str
    arguments: str  # JSON string
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def type(self) -> str:
        return "function"

    @property
    def function(self) -> ToolCall:
        """Return self so ``tc.function.name`` / ``tc.function.arguments`` work.

        Backward-compat shim mirroring the OpenAI SDK's nested ``function`` object,
        kept so callers that read ``tc.function.name`` keep working against the
        flat canonical fields.
        """
        return self


@dataclass
class Usage:
    """Token usage from an API response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class NormalizedResponse:
    """Normalized API response from any provider.

    Shared fields are truly cross-provider -- every caller can rely on them
    without branching on api_mode. Protocol-specific state goes in
    ``provider_data`` so that only protocol-aware code paths read it.
    """

    content: str | None
    tool_calls: list[ToolCall] | None
    finish_reason: str  # "stop", "tool_calls", "length", "content_filter"
    reasoning: str | None = None
    usage: Usage | None = None
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def reasoning_content(self) -> str | None:
        pd = self.provider_data or {}
        return pd.get("reasoning_content")

    @property
    def reasoning_details(self):
        pd = self.provider_data or {}
        return pd.get("reasoning_details")


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def build_tool_call(
    id: str | None,
    name: str,
    arguments: Any,
    **provider_fields: Any,
) -> ToolCall:
    """Build a ``ToolCall``, auto-serialising *arguments* if it's a dict.

    Any extra keyword arguments are collected into ``provider_data``.
    """
    args_str = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
    pd = dict(provider_fields) if provider_fields else None
    return ToolCall(id=id, name=name, arguments=args_str, provider_data=pd)


def map_finish_reason(reason: str | None, mapping: dict[str, str]) -> str:
    """Translate a provider-specific stop reason to the normalised set.

    Falls back to ``"stop"`` for unknown or ``None`` reasons.
    """
    if reason is None:
        return "stop"
    return mapping.get(reason, "stop")

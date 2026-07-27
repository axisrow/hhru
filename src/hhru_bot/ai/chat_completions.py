# Ported from NousResearch/hermes-agent (MIT).
# Copyright (c) 2025 Nous Research.
#
"""OpenAI Chat Completions transport.

Handles the default api_mode ('chat_completions') used by OpenAI-compatible
providers (OpenAI, OpenRouter, Nous, local servers, etc.).

Messages and tools are already in OpenAI format -- ``convert_messages`` and
``convert_tools`` are near-identity. The complexity lives in ``build_kwargs``
(provider-specific conditionals) and in ``normalize_response``.

This is a deliberately trimmed port of hermes-agent's chat_completions
transport: provider-specific reasoning/thinking/gemini/moonshot/lmstudio and
the ProviderProfile path were removed. hhru uses a single OpenAI-compatible
endpoint; the core conversion/normalization surface is preserved verbatim.
"""

from __future__ import annotations

from typing import Any

from .base import ProviderTransport
from .types import NormalizedResponse, ToolCall, Usage

# Models that expect the ``developer`` role instead of ``system``. Ported from
# agent/prompt_builder.py:664 (DEVELOPER_ROLE_MODELS) -- kept as a local
# constant so this transport does not pull in the rest of hermes' prompt
# machinery (hermes_constants / runtime_cwd / skill_utils / threat_patterns).
DEVELOPER_ROLE_MODELS = ("gpt-5", "codex")


class ChatCompletionsTransport(ProviderTransport):
    """Transport for api_mode='chat_completions'.

    The default path for OpenAI-compatible providers.
    """

    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(self, messages: list[dict[str, Any]], **kwargs) -> list[dict[str, Any]]:
        """Messages are already in OpenAI format -- strip internal fields that
        strict chat-completions providers reject with HTTP 400/422:

        - Codex Responses API fields: ``codex_reasoning_items`` /
          ``codex_message_items`` on the message.
        - ``tool_name`` on tool-result messages (SQLite FTS bookkeeping, not
          part of the Chat Completions schema).
        - ``effect_disposition`` / ``timestamp`` / ``api_content`` -- internal
          sidecars.
        - Any Hermes-internal scaffolding marker -- any top-level message key
          starting with ``_``. Permissive providers silently drop unknown
          message keys, but strict gateways reject them.
        """
        needs_sanitize = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if (
                "codex_reasoning_items" in msg
                or "codex_message_items" in msg
                or "tool_name" in msg
                or "effect_disposition" in msg
                or "timestamp" in msg
                or "api_content" in msg
            ):
                needs_sanitize = True
                break
            if any(isinstance(k, str) and k.startswith("_") for k in msg):
                needs_sanitize = True
                break

        if not needs_sanitize:
            return messages

        sanitized = list(messages)
        for msg_idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue

            copied_msg: dict[str, Any] | None = None

            # NOTE: mutable_msg is redefined per loop iteration, so each closure
            # binds its own msg / msg_idx -- there is no late-binding bug. The
            # inline noqa markers below silence ruff's conservative B023 warning.
            def mutable_msg() -> dict[str, Any]:
                nonlocal copied_msg
                if copied_msg is None:
                    copied_msg = dict(msg)  # noqa: B023
                    sanitized[msg_idx] = copied_msg  # noqa: B023
                return copied_msg

            if (
                "codex_reasoning_items" in msg
                or "codex_message_items" in msg
                or "tool_name" in msg
                or "effect_disposition" in msg
                or "timestamp" in msg
                or "api_content" in msg
            ):
                out_msg = mutable_msg()
                out_msg.pop("codex_reasoning_items", None)
                out_msg.pop("codex_message_items", None)
                out_msg.pop("tool_name", None)
                out_msg.pop("effect_disposition", None)
                out_msg.pop("timestamp", None)
                out_msg.pop("api_content", None)

            # Drop all internal scaffolding markers (``_``-prefixed).
            internal_keys = [k for k in msg if isinstance(k, str) and k.startswith("_")]
            if internal_keys:
                out_msg = mutable_msg()
                for key in internal_keys:
                    out_msg.pop(key, None)
        return sanitized

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tools are already in OpenAI format -- identity."""
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params,
    ) -> dict[str, Any]:
        """Build ``chat.completions.create()`` kwargs.

        params (all optional):
            timeout: float -- API call timeout
            max_tokens: int | None -- user-configured max tokens
            request_overrides: dict | None -- caller-supplied kwargs merged last
            temperature: Any -- forwarded when present
            extra_body: dict | None -- folded into api_kwargs['extra_body']
        """
        # Sanitize: drop reasoning_items / tool_name / internal markers.
        sanitized = self.convert_messages(messages, model=model)

        # Developer role swap for GPT-5/Codex models.
        model_lower = params.get("model_lower", (model or "").lower())
        if (
            sanitized
            and isinstance(sanitized[0], dict)
            and sanitized[0].get("role") == "system"
            and any(p in model_lower for p in DEVELOPER_ROLE_MODELS)
        ):
            sanitized = list(sanitized)
            sanitized[0] = {**sanitized[0], "role": "developer"}

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": sanitized,
        }

        timeout = params.get("timeout")
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        # Temperature
        temperature = params.get("temperature")
        if temperature is not None:
            api_kwargs["temperature"] = temperature

        # Tools
        if tools:
            api_kwargs["tools"] = tools

        # max_tokens -- user-configured value wins when present.
        max_tokens = params.get("max_tokens")
        if max_tokens is not None:
            api_kwargs["max_tokens"] = max_tokens

        # extra_body assembly
        extra_body: dict[str, Any] = {}
        additions = params.get("extra_body")
        if additions:
            extra_body.update(additions)
        if extra_body:
            api_kwargs["extra_body"] = extra_body

        # Request overrides last (service_tier etc.)
        overrides = params.get("request_overrides")
        if overrides:
            api_kwargs.update(overrides)

        return api_kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize OpenAI ChatCompletion to NormalizedResponse.

        For chat_completions this is near-identity -- the response is already in
        OpenAI format. ``reasoning_details`` (OpenRouter unified format) and
        ``reasoning_content`` (DeepSeek/Moonshot) are preserved for downstream
        replay.
        """
        choice = response.choices[0]
        msg = choice.message
        # Some gateways return an integer finish_reason instead of a string.
        _fr = choice.finish_reason
        if isinstance(_fr, int):
            _fr = str(_fr)
        finish_reason = _fr or "stop"

        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    )
                )

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
            )

        # Preserve reasoning fields separately. DeepSeek/Moonshot use
        # ``reasoning_content``; others use ``reasoning``. Keep them apart in
        # provider_data rather than merging.
        reasoning = getattr(msg, "reasoning", None)
        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content is None and hasattr(msg, "model_extra"):
            model_extra = getattr(msg, "model_extra", None) or {}
            if isinstance(model_extra, dict) and "reasoning_content" in model_extra:
                reasoning_content = model_extra["reasoning_content"]

        provider_data: dict[str, Any] = {}
        if reasoning_content is not None:
            provider_data["reasoning_content"] = reasoning_content
        rd = getattr(msg, "reasoning_details", None)
        if rd:
            provider_data["reasoning_details"] = rd

        # OpenAI structured-refusal field. When a model declines, the SDK
        # populates ``message.refusal`` and leaves ``content`` empty. Promote it
        # to content + a ``content_filter`` finish reason so a refusal doesn't
        # look like an empty response. ``refusal`` is ``None`` for normal
        # responses, so this is a no-op in the common case.
        content = msg.content
        refusal = getattr(msg, "refusal", None)
        if refusal is None and hasattr(msg, "model_extra"):
            _msg_extra = getattr(msg, "model_extra", None) or {}
            if isinstance(_msg_extra, dict):
                refusal = _msg_extra.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            provider_data["refusal"] = refusal
            _has_text = isinstance(content, str) and content.strip()
            _has_tool_calls = bool(tool_calls)
            # Only promote to a terminal content_filter when the refusal is the
            # sole payload -- no visible text and no tool calls.
            if not _has_text and not _has_tool_calls:
                content = refusal
                if finish_reason in (None, "stop"):
                    finish_reason = "content_filter"

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """Check that response has valid choices."""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        if not response.choices:
            return False
        return True

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        """Extract cache stats from prompt_tokens_details (OpenRouter/OpenAI)
        or DeepSeek's native top-level prompt_cache_hit_tokens field."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        if details:
            cached = getattr(details, "cached_tokens", 0) or 0
            written = getattr(details, "cache_write_tokens", 0) or 0
        else:
            cached = 0
            written = 0
        if not cached:
            # DeepSeek native API shape: top-level prompt_cache_hit_tokens.
            cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None


# Auto-register on import
from .registry import register_transport  # noqa: E402

register_transport("chat_completions", ChatCompletionsTransport)

# Ported from NousResearch/hermes-agent (MIT).
# Copyright (c) 2025 Nous Research.
#
"""Runtime provider resolution for the AI transport.

This is a deliberately thin port of hermes-agent's ``resolve_runtime_provider``.
The original resolves credentials across ~20 providers via credential pools,
OAuth, Vertex, Bedrock, Azure Foundry, and a trust-gated custom-provider
chain. hhru needs none of that: it talks to one OpenAI-compatible endpoint
configured in ``config.yaml`` (section ``ai``) with a key from the environment.

The output dict shape mirrors the original so a future richer resolver could
drop in: ``provider``, ``api_mode``, ``base_url``, ``api_key``, ``model``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config_sections.ai import AiConfig

# Responses is the supported API mode. The explicit override remains for
# callers migrating from the legacy chat-completions transport.
DEFAULT_API_MODE = "responses"

# Env var that carries the API key. The key is intentionally NOT read from
# config.yaml so it can't be committed by accident.
API_KEY_ENV_VAR = "HHRU_AI_API_KEY"


def _resolve_api_key(explicit_api_key: str | None) -> str:
    """Pick the API key: explicit argument first, then env, else empty string.

    An empty string is returned (not raised) so resolution never fails on
    missing credentials -- failure surfaces on the first real request where the
    SDK rejects the empty key.
    """
    if explicit_api_key and explicit_api_key.strip():
        return explicit_api_key.strip()
    env_value = os.environ.get(API_KEY_ENV_VAR, "")
    if env_value:
        return env_value.strip()
    return ""


def resolve_runtime_provider(
    config: AiConfig,
    *,
    api_key: str | None = None,
    api_mode: str | None = None,
) -> dict[str, Any]:
    """Resolve a runtime provider entry from ``AiConfig`` + env api key.

    Args:
        config: parsed top-level ``ai`` config (provider / model / base_url).
        api_key: optional explicit key; overrides the ``HHRU_AI_API_KEY`` env var.
        api_mode: optional override; defaults to ``chat_completions``.

    Returns a dict with ``provider``, ``api_mode``, ``base_url``, ``api_key``,
    ``model`` and ``source`` (``"config"`` when the key came from config/env,
    ``"explicit"`` when passed in directly).
    """
    resolved_key = _resolve_api_key(api_key)
    source = "explicit" if api_key and api_key.strip() else "config"
    return {
        "provider": config.provider,
        "api_mode": api_mode or DEFAULT_API_MODE,
        "base_url": config.base_url,
        "api_key": resolved_key,
        "model": config.model,
        "source": source,
    }

"""Runtime provider resolution for the AI transport.

hhru хранит только metadata включённой AI-интеграции. Выбор endpoint,
credentials и fallback chain принадлежат ``hermes-agent-axisrow``:
``LLMClient`` передаёт их в ``agent.auxiliary_client.call_llm`` без
переопределений.

Форма выходного dict сохраняет ``provider``, ``api_mode``, ``base_url`` и
``model`` для диагностики; это не runtime credentials.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config_sections.ai import AiConfig

# Единственный режим транспорта после #230: Responses API (Chat Completions
# устарел — решение пользователя). Совпадает с llm_client.API_MODE.
DEFAULT_API_MODE = "codex_responses"


def resolve_runtime_provider(
    config: AiConfig,
    *,
    api_mode: str | None = None,
) -> dict[str, Any]:
    """Return diagnostic metadata from ``AiConfig``.

    Args:
        config: parsed top-level ``ai`` config (provider / model / base_url).
        api_mode: optional override; defaults to ``codex_responses``.

    Credentials are intentionally excluded: Hermes owns them through its own
    auth/config resolver and hhru must not read or override that state.
    """
    return {
        "provider": config.provider,
        "api_mode": api_mode or DEFAULT_API_MODE,
        "base_url": config.base_url,
        "model": config.model,
    }

# Ported from NousResearch/hermes-agent (MIT) -- transport conversion contract.
# The ``LLMClient`` wrapper itself is new for hhru.
#
"""Thin LLM client over the ``openai`` Responses API.

``openai`` is an OPTIONAL dependency (install group ``[ai]``). It is imported
lazily inside the methods so that importing this package -- and the rest of
``hhru_bot`` -- does not require it. A clear ``ImportError`` is raised only
when an LLM call is actually attempted without the SDK installed.

The client delegates format conversion / request kwargs assembly / response
normalization to the registered transport (``chat_completions`` by default).
It owns only client construction and the SDK call -- no retry, streaming,
credential refresh, or prompt caching (those concerns belong elsewhere).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .registry import get_transport
from .runtime_provider import resolve_runtime_provider
from .types import NormalizedResponse

if TYPE_CHECKING:
    from ..config_sections.ai import AiConfig


class LLMClient:
    """Minimal synchronous client for an OpenAI-compatible Responses endpoint.

    Args:
        ai_config: parsed top-level ``ai`` config (provider / model / base_url).
        api_key: optional explicit key; otherwise read from ``HHRU_AI_API_KEY``.
        api_mode: transport to use; defaults to ``responses``.
    """

    def __init__(
        self,
        ai_config: AiConfig,
        *,
        api_key: str | None = None,
        api_mode: str | None = None,
    ) -> None:
        self._config = ai_config
        self._runtime = resolve_runtime_provider(ai_config, api_key=api_key, api_mode=api_mode)
        self._api_mode = self._runtime["api_mode"]
        # The OpenAI client owns a persistent httpx connection pool; construct it
        # once and reuse it across calls rather than paying pool setup per chat().
        # ``openai`` is imported lazily here (not at module import) so the package
        # stays importable without the optional [ai] extra installed.
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "openai SDK is required for LLM calls. Install it with: "
                "pip install -e '.[ai]'  (or: pip install openai)"
            ) from e
        self._client = OpenAI(
            base_url=self._runtime["base_url"],
            api_key=self._runtime["api_key"],
        )

    @property
    def runtime(self) -> dict[str, Any]:
        """Resolved runtime entry (provider / api_mode / base_url / api_key / model)."""
        return self._runtime

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> NormalizedResponse:
        """Run a Responses API request and return a NormalizedResponse.

        Extra keyword arguments are forwarded to the transport's ``build_kwargs``
        (e.g. ``temperature``, ``max_tokens``, ``timeout``). ``openai`` SDK
        exceptions propagate to the caller unchanged -- this layer does not retry.
        """
        transport = get_transport(self._api_mode)
        if transport is None:
            raise RuntimeError(
                f"No transport registered for api_mode={self._api_mode!r}. "
                f"Install the corresponding transport module or check ai_mode."
            )

        kwargs = transport.build_kwargs(
            self._runtime["model"],
            messages,
            tools,
            **params,
        )
        response = self._client.responses.create(**kwargs)
        return transport.normalize_response(response)

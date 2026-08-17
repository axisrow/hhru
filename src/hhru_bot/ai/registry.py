# Ported from NousResearch/hermes-agent (MIT).
# Copyright (c) 2025 Nous Research.
#
"""Transport registry for provider response normalization.

Usage:
    from hhru_bot.ai import get_transport
    transport = get_transport("chat_completions")
    result = transport.normalize_response(raw_response)

Transports auto-register on import of their module (see
``chat_completions.py``). ``get_transport`` lazily imports the bundled
transport module on first lookup so callers don't pay the import cost until
they actually resolve a transport.
"""

from __future__ import annotations

from typing import Any

_REGISTRY: dict[str, type] = {}
_discovered: bool = False


def register_transport(api_mode: str, transport_cls: type) -> None:
    """Register a transport class for an api_mode string."""
    _REGISTRY[api_mode] = transport_cls


def get_transport(api_mode: str) -> Any:
    """Get a transport instance for the given api_mode.

    Returns None if no transport is registered for this api_mode. This allows
    gradual migration -- call sites can check for None and fall back.
    """
    global _discovered
    if not _discovered:
        _discover_transports()
    cls = _REGISTRY.get(api_mode)
    if cls is None:
        # The registry can be partially populated when a specific transport
        # module was imported directly. Discover on misses, not only when the
        # registry is empty, so test/order-dependent imports do not make valid
        # api_modes unavailable.
        _discover_transports()
        cls = _REGISTRY.get(api_mode)
    if cls is None:
        return None
    return cls()


def _discover_transports() -> None:
    """Import all bundled transport modules to trigger auto-registration."""
    global _discovered
    _discovered = True
    for module in ("chat_completions", "responses"):
        try:
            __import__(f"{__package__}.{module}")
        except ImportError:
            pass

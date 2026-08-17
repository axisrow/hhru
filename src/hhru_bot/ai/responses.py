"""OpenAI Responses API transport used by the optional AI integration.

The adapter deliberately depends only on the public ``openai`` Responses
surface.  ``hermes-agent-axisrow`` is an optional companion dependency, but
its internal modules are not imported: they are not a stable library API.
"""

from __future__ import annotations

import json
from typing import Any

from .base import ProviderTransport
from .registry import register_transport
from .types import NormalizedResponse, ToolCall, Usage


class ResponsesTransport(ProviderTransport):
    """Convert the existing message contract to Responses API arguments."""

    @property
    def api_mode(self) -> str:
        return "responses"

    def convert_messages(self, messages: list[dict[str, Any]], **kwargs) -> list[dict[str, Any]]:
        del kwargs
        return [
            {key: value for key, value in message.items() if key in {"role", "content"}}
            for message in messages
            if isinstance(message, dict)
        ]

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for tool in tools:
            function = tool.get("function", tool)
            converted_tool = {
                "type": "function",
                "name": function["name"],
            }
            if function.get("description"):
                converted_tool["description"] = function["description"]
            if function.get("parameters"):
                converted_tool["parameters"] = function["parameters"]
            converted.append(converted_tool)
        return converted

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": model, "input": self.convert_messages(messages)}
        if tools:
            kwargs["tools"] = self.convert_tools(tools)
        for source, target in (
            ("max_tokens", "max_output_tokens"),
            ("temperature", "temperature"),
            ("timeout", "timeout"),
        ):
            if params.get(source) is not None:
                kwargs[target] = params[source]
        overrides = params.get("request_overrides")
        if overrides:
            kwargs.update(overrides)
        return kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        del kwargs
        tool_calls = []
        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) == "function_call":
                arguments = getattr(item, "arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments)
                tool_calls.append(ToolCall(getattr(item, "call_id", None), item.name, arguments))
        usage_obj = getattr(response, "usage", None)
        usage = None
        if usage_obj:
            usage = Usage(
                prompt_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
                completion_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
                total_tokens=getattr(usage_obj, "total_tokens", 0) or 0,
            )
        content = getattr(response, "output_text", None)
        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls or None,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=usage,
        )

register_transport("responses", ResponsesTransport)

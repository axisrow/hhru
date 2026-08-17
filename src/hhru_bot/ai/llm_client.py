"""Тонкий LLM-клиент на Responses API (openai SDK) — issue #230.

Интерфейс Responses API, не Chat Completions (устарел — решение в #230).
Точка интеграции — сам ``openai`` SDK: фаза 1 ишью показала, что
hermes-agent-axisrow не SDK, и для одного изолированного вызова даёт
только обёртку над тем же openai SDK, который сам тянет зависимостью
(пин openai==2.24.0). In-process импорт форка отвергнут: 40+ тяжёлых
депсов, generic top-level модули (``agent``/``utils``/``cli``) в
site-packages всего venv, побочные эффекты импорта.

``openai`` — опциональная зависимость (группа ``[ai]``), импортируется
лениво в конструкторе: ImportError поднимается только при реальной
попытке построить клиента — команды ловят его и откатываются на не-AI
режим (commands/_common.py).

Ключ передаётся в SDK всегда строкой (пустой, если не задан): при
``None`` SDK полез бы в env ``OPENAI_API_KEY`` — подсос чужих кредов нам
не нужен. Пустой ключ падает 401 на первом запросе → fallback
потребителя (семантика прежнего chat-транспорта).

Клиент владеет только построением SDK-клиента и конвертацией формата;
ретраев, fallback-провайдеров и refresh-токенов здесь нет намеренно —
устойчивость обеспечивают потребители (fallback на шаблон в
ai/letters.py, эвристика в scoring.py).

``store=False``: сгенерированные письма не должны оседать у провайдера
(Responses API по умолчанию хранит ответы 30 дней).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .runtime_provider import resolve_runtime_provider
from .types import NormalizedResponse, ToolCall, Usage

if TYPE_CHECKING:
    from ..config_sections.ai import AiConfig


class LLMClient:
    """Минимальный синхронный клиент на Responses API.

    Args:
        ai_config: распарсенная корневая секция ``ai`` (provider/model/base_url).
        api_key: явный ключ; иначе читается из ``HHRU_AI_API_KEY``.
    """

    def __init__(
        self,
        ai_config: AiConfig,
        *,
        api_key: str | None = None,
    ) -> None:
        self._config = ai_config
        self._runtime = resolve_runtime_provider(ai_config, api_key=api_key)
        # Ленивый импорт (не на уровне модуля): без группы [ai] пакет
        # hhru_bot и его потребители должны импортироваться как раньше.
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
        """Один вызов Responses API → NormalizedResponse.

        Контракт сохранён с chat-транспорта #16 (потребители letters/scoring
        не меняются): chat-сообщения, ``temperature``/``timeout``/``max_tokens``
        в params. Конвертация: system → ``instructions``, остальные →
        ``input``; ``max_tokens`` → ``max_output_tokens``. Исключения SDK
        прокидываются вызывающей стороне неизменными.
        """
        instructions, input_items = _split_instructions(messages)
        kwargs: dict[str, Any] = {
            "model": self._runtime["model"],
            "input": input_items,
            "store": False,
        }
        if instructions is not None:
            kwargs["instructions"] = instructions
        max_tokens = params.pop("max_tokens", None)
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = _convert_tools(tools)
        kwargs.update(params)
        response = self._client.responses.create(**kwargs)
        return _normalize_response(response)


def _split_instructions(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Разделить chat-сообщения на Responses-пару (instructions, input).

    ``system``-сообщение становится ``instructions`` (Responses API не
    принимает role=system в ``input``); остальные роли проходят как есть.
    Несколько system-сообщений склеиваются переводом строки.
    """
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content:
                instructions.append(content)
        else:
            input_items.append(msg)
    joined = "\n".join(instructions) if instructions else None
    return joined, input_items


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat-формат tools → Responses-формат (плоский function-tool)."""
    converted = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        converted.append(
            {
                "type": "function",
                "name": fn.get("name"),
                "parameters": fn.get("parameters"),
            }
        )
    return converted


def _normalize_response(response: Any) -> NormalizedResponse:
    """Ответ Responses API (типизированный объект SDK) → NormalizedResponse.

    Маппинг: ``status`` → finish_reason (completed → stop;
    incomplete/max_output_tokens → length; incomplete/content_filter →
    content_filter); текст — агрегация ``output_text``-блоков;
    refusal-блок → content_filter при единственной payload (семантика
    экс-порта #16); ``usage.input_tokens/output_tokens`` → Usage.
    ``status=failed`` — ошибка протокола, поднимаем исключением:
    это устоявшийся путь отказа для потребителей (fallback).
    """
    status = getattr(response, "status", None)
    if status == "failed":
        error = getattr(response, "error", None)
        raise RuntimeError(f"Responses API вернул status=failed: {error}")

    finish_reason = "stop"
    if status == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
        if reason == "max_output_tokens":
            finish_reason = "length"
        elif reason == "content_filter":
            finish_reason = "content_filter"

    texts: list[str] = []
    refusal: str | None = None
    tool_calls: list[ToolCall] | None = None
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            for block in getattr(item, "content", None) or []:
                block_type = getattr(block, "type", None)
                if block_type == "output_text":
                    texts.append(getattr(block, "text", "") or "")
                elif block_type == "refusal" and refusal is None:
                    refusal = getattr(block, "refusal", None)
        elif item_type == "function_call":
            if tool_calls is None:
                tool_calls = []
            tool_calls.append(
                ToolCall(
                    id=getattr(item, "call_id", None),
                    name=getattr(item, "name", ""),
                    arguments=getattr(item, "arguments", "{}"),
                )
            )
    content: str | None = "".join(texts) or None

    provider_data: dict[str, Any] = {}
    if isinstance(refusal, str) and refusal.strip():
        provider_data["refusal"] = refusal
        # Refusal — единственная payload (нет текста и tool_calls) →
        # показываем его как контент с content_filter, чтобы отказ модели
        # не выглядел пустым успехом.
        if not (content and content.strip()) and not tool_calls:
            content = refusal
            if finish_reason == "stop":
                finish_reason = "content_filter"

    usage = None
    u = getattr(response, "usage", None)
    if u is not None:
        usage = Usage(
            prompt_tokens=getattr(u, "input_tokens", 0) or 0,
            completion_tokens=getattr(u, "output_tokens", 0) or 0,
            total_tokens=getattr(u, "total_tokens", 0) or 0,
        )

    return NormalizedResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        provider_data=provider_data or None,
    )

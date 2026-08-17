# Обёртка над pip-зависимостью hermes-agent-axisrow (issue #230). Заменяет
# самодельный замороженный порт из NousResearch/hermes-agent (#16/#96):
# конвертацию chat→Responses теперь делает сам пакет, hhru владеет только
# построением клиента и нормализацией ответа.
#
"""Тонкий LLM-клиент поверх ``hermes-agent-axisrow`` (Responses API).

``hermes-agent-axisrow`` — опциональная зависимость (группа ``[ai]``),
импортируется лениво в конструкторе, чтобы остальной код импортировался
без неё. ImportError поднимается только при реальной попытке построить
клиента — команды ловят его и откатываются на не-AI режим (см.
commands/_common.py).

Точка интеграции выбрана фазой 1 ишью #230 (живые проверки, не догадки):
``agent.auxiliary_client.call_llm`` — центральный resolver chain пакета.
Он использует настроенные Hermes provider/auth/fallback цепочки (включая
OpenRouter и Nous Portal); hhru не читает и не изменяет ``~/.hermes``.
Если задан ``HHRU_AI_API_KEY``, hhru передаёт свой endpoint/key как
``main_runtime`` — это первый маршрут. Без ключа вызов намеренно остаётся
полностью на конфигурации и credentials Hermes.

Маршрут не должен быть молчаливым: Hermes заполняет ``route_info`` реальным
provider/model, в том числе после fallback. Мы пишем его в лог и в
``NormalizedResponse.provider_data``. Поэтому журнал hhru показывает, какой
из провайдеров цепочки фактически ответил, не раскрывая ключи.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .runtime_provider import resolve_runtime_provider
from .types import NormalizedResponse, ToolCall, Usage

if TYPE_CHECKING:
    from ..config_sections.ai import AiConfig

# Responses API для явно переданного hhru runtime. Hermes выбирает формат
# самостоятельно для fallback-провайдеров, чтобы не ломать их transport.
API_MODE = "codex_responses"
_TASK_NAME = "hhru"

logger = logging.getLogger("hhru_bot.ai.llm_client")


class LLMClient:
    """Минимальный синхронный клиент на Responses API через hermes-форк.

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
            from agent.auxiliary_client import call_llm
        except ImportError as e:
            raise ImportError(
                "hermes-agent-axisrow is required for LLM calls. Install it with: "
                "pip install -e '.[ai]'  (or: pip install hermes-agent-axisrow)"
            ) from e

        self._call_llm = call_llm
        self._main_runtime = _hhru_main_runtime(self._runtime)

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
        """Один вызов через полный Hermes resolver chain → NormalizedResponse."""
        route_info: dict[str, str] = {}
        kwargs: dict[str, Any] = {
            "task": _TASK_NAME,
            "messages": messages,
            "route_info": route_info,
        }
        if self._main_runtime is not None:
            kwargs["main_runtime"] = self._main_runtime
        if tools:
            kwargs["tools"] = tools
        for name in ("temperature", "max_tokens", "timeout", "extra_body"):
            if name in params:
                kwargs[name] = params[name]
        response = self._call_llm(**kwargs)
        provider = route_info.get("provider", "unknown")
        model = route_info.get("model", "unknown")
        logger.info("Hermes AI request completed via provider=%s model=%s", provider, model)
        return _normalize_response(response, route_info=route_info)


def _hhru_main_runtime(runtime: dict[str, Any]) -> dict[str, str] | None:
    """Make the hhru credential the first Hermes route only when configured.

    An absent ``HHRU_AI_API_KEY`` deliberately returns ``None``: then
    ``call_llm`` reads the user's existing Hermes auth/config and follows its
    configured fallback chain without hhru creating or mutating any of it.
    """
    key = runtime.get("api_key")
    if not isinstance(key, str) or not key:
        return None
    return {
        "provider": str(runtime["provider"]),
        "model": str(runtime["model"]),
        "base_url": str(runtime["base_url"]),
        "api_key": key,
        "api_mode": API_MODE,
    }


def _normalize_response(
    response: Any,
    *,
    route_info: dict[str, str] | None = None,
) -> NormalizedResponse:
    """Chat-подобный ответ адаптера → NormalizedResponse.

    Ответ ``CodexAuxiliaryClient`` повторяет форму ChatCompletion
    (choices[0].message / usage), поэтому нормализация — та же логика,
    что была в удалённом порте chat_completions (#16): коэрсция
    finish_reason, refusal→content_filter, tool_calls, usage.
    """
    choice = response.choices[0]
    msg = choice.message
    # Некоторые шлюзы возвращают числовой finish_reason.
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
    if route_info:
        provider_data["hermes_route"] = dict(route_info)

    # Structured-refusal: пустой content + refusal → content_filter, чтобы
    # отказ модели не выглядел как «пустой успешный ответ» (потребители
    # letters/scoring отличают пустой контент и уходят в fallback).
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

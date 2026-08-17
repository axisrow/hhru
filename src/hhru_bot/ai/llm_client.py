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
``agent.auxiliary_client.resolve_provider_client`` — публичный «central
router» пакета. Вызов с ``provider="custom"`` и ЯВНЫМИ
``explicit_base_url``/``explicit_api_key``/``model`` строит клиента ровно
на указанном endpoint и ``api_mode="codex_responses"`` оборачивает его в
``CodexAuxiliaryClient``: ``.chat.completions.create(**kwargs)`` принимает
chat-completions kwargs, под капотом конвертирует в Responses API и
возвращает ответ в chat-подобной форме (choices[0].message.content,
finish_reason, usage).

Почему НЕ ``call_llm`` из того же модуля: живой тест фазы 1 показал, что
при connection error он молча переключается на основной аккаунт hermes
пользователя (fallback-цепочка «aux-задача не падает любой ценой»). Для
hhru это потеря контроля расходов. ``resolve_provider_client`` уровнем
ниже — fallback-цикла там нет, транспорт hhru дёргает ``create()``
напрямую, и исключение доходит до потребителя как есть. Ретраев здесь
тоже нет намеренно: устойчивость обеспечивают потребители (fallback на
шаблон в ai/letters.py, эвристика в scoring.py).

Ключи: только явный аргумент или env ``HHRU_AI_API_KEY``
(runtime_provider). Пустой ключ передаётся как ``no-key-required`` —
локальные серверы (Ollama/LM Studio) его игнорируют, а удалённые
endpoint'ы честно падают 401 на первом запросе (уходит в fallback
потребителя). Это закрывает и подсос ключей из окружения hermes: без
плейсхолдера пустой explicit-ключ заставил бы resolver искать
``OPENAI_API_KEY`` и креды ``~/.hermes`` (фаза 1, ветка custom).

Известное ограничение провода: Responses-адаптер пакета не передаёт
``temperature``/``max_tokens`` (Codex-эндпоинт отвергает их с 400).
Параметры принимаются для совместимости интерфейса, но на проводах
молча игнорируются; ``timeout`` и ``tools`` форвардятся.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .runtime_provider import resolve_runtime_provider
from .types import NormalizedResponse, ToolCall, Usage

if TYPE_CHECKING:
    from ..config_sections.ai import AiConfig

# Локальные серверы не требуют авторизации; SDK требует непустой ключ.
# См. модульный докстринг: плейсхолдер заодно пресекает подсос кредов
# hermes при пустом HHRU_AI_API_KEY.
_PLACEHOLDER_KEY = "no-key-required"

# Единственный api_mode транспорта: Responses API (Chat Completions в
# hhru больше не используется — решение пользователя в #230).
API_MODE = "codex_responses"


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
            from agent.auxiliary_client import resolve_provider_client
        except ImportError as e:
            raise ImportError(
                "hermes-agent-axisrow is required for LLM calls. Install it with: "
                "pip install -e '.[ai]'  (or: pip install hermes-agent-axisrow)"
            ) from e

        key = self._runtime["api_key"] or _PLACEHOLDER_KEY
        client, model = resolve_provider_client(
            "custom",
            model=self._runtime["model"],
            explicit_base_url=self._runtime["base_url"],
            explicit_api_key=key,
            api_mode=API_MODE,
        )
        if client is None:
            # С явными base_url/key resolver не находит креды только при
            # битом base_url — это ошибка конфигурации, падаем громко.
            raise RuntimeError(
                f"hermes resolve_provider_client вернул None для endpoint "
                f"{self._runtime['base_url']!r} — проверьте секцию ai в config.yaml"
            )
        self._client = client
        self._model = model

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
        """Один LLM-вызов (Responses API под капотом) → NormalizedResponse.

        Дополнительные kwargs (``temperature``, ``timeout``, ``max_tokens``)
        форвардятся в ``create()`` адаптера; исключения SDK прокидываются
        вызывающей стороне неизменными — этот слой не ретраит и не падает
        на другие провайдеры (контроль расходов, #230).
        """
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        kwargs.update(params)
        response = self._client.chat.completions.create(**kwargs)
        return _normalize_response(response)


def _normalize_response(response: Any) -> NormalizedResponse:
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

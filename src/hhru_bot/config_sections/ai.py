"""Парсер TOP-LEVEL секции ai → AiConfig (issue #16, Этап 5).

В отличие от resume-подсекций (search/scoring/ai_profile) эта секция —
корневая, на уровне account/throttle: один LLM-провайдер на весь бот, а не
своё у каждого резюме. Поэтому парсится напрямую в ``load_config`` (как
``parse_account``), а НЕ через resume-реестр ``config_sections/_registry``.

Секция опциональна: при отсутствии ``load_config`` оставит
``AppConfig.ai = None`` (обратная совместимость — бот без AI работает как
раньше).

API-ключ намеренно НЕ парсится из yaml — только provider/model/base_url.
Ключ читается из env ``HHRU_AI_API_KEY`` в момент реального LLM-вызова
(см. ``hhru_bot.ai.runtime_provider``), чтобы секрет не попадал в конфиг и
не рисковал утечь в git-коммит.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ConfigError


@dataclass(frozen=True)
class AiConfig:
    """Конфигурация LLM-провайдера (OpenAI-совместимый endpoint).

    provider: имя провайдера для логов/метаданных (напр. 'openai', 'openrouter').
    model: имя модели, отправляемое в API (напр. 'gpt-4o').
    base_url: корневой URL OpenAI-совместимого API (напр. 'https://api.openai.com/v1').
    """

    provider: str
    model: str
    base_url: str


def _require(mapping: dict, key: str, context: str):
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"В конфиге отсутствует обязательное поле '{key}' ({context})")
    return mapping[key]


def parse_ai(raw, context: str) -> AiConfig | None:
    """raw — корневая секция ai. Возвращает AiConfig или None, если секции нет.

    ``context`` — строка для диагностики ошибок (обычно 'ai').
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    provider = _require(raw, "provider", context)
    model = _require(raw, "model", context)
    base_url = _require(raw, "base_url", context)
    for name, value in (("provider", provider), ("model", model), ("base_url", base_url)):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"Поле '{name}' ({context}) должно быть непустой строкой, получено: {value!r}"
            )
    return AiConfig(
        provider=provider.strip(),
        model=model.strip(),
        base_url=base_url.strip(),
    )


# ai — корневая секция (как account), не resume-подсекция, поэтому в resume-реестр
# не регистрируется; используется напрямую из load_config.

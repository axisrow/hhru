"""Парсер TOP-LEVEL секции ai → AiConfig (issue #16, Этап 5).

В отличие от resume-подсекций (search/scoring/ai_profile) эта секция —
корневая, на уровне account/throttle: один LLM-провайдер на весь бот, а не
своё у каждого резюме. Поэтому парсится напрямую в ``load_config`` (как
``parse_account``), а НЕ через resume-реестр ``config_sections/_registry``.

Секция опциональна: при отсутствии ``load_config`` оставит
``AppConfig.ai = None`` (обратная совместимость — бот без AI работает как
раньше).

API-ключ намеренно НЕ парсится из yaml. Credentials и provider fallback
настраиваются и хранятся только Hermes; hhru не читает и не изменяет их.
Наличие секции ``ai`` включает AI-функциональность (letters/scoring); поля
``provider``/``model``/``base_url`` больше НЕ управляют маршрутизацией
(Responses API через ``hermes-agent-axisrow``, issue #230) — они остались
как устаревшая метаданная и, если заданы, выдают deprecation-warning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import ConfigError

logger = logging.getLogger("hhru_bot.config_sections.ai")


@dataclass(frozen=True)
class AiConfig:
    """Конфигурация AI-интеграции.

    Присутствие секции включает AI; сами поля — устаревшие метаданные.
    Маршрутизация (provider/model/base_url/credentials) принадлежит Hermes.

    provider: устаревшее, не влияет на запрос (для логов/диагностики).
    model: устаревшее, не влияет на запрос (реальную модель решает Hermes chain).
    base_url: устаревшее, не влияет на запрос.
    """

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None


def _opt_str(mapping: dict, key: str, context: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"Поле '{key}' ({context}) должно быть непустой строкой, получено: {value!r}"
        )
    return value.strip()


def parse_ai(raw, context: str) -> AiConfig | None:
    """raw — корневая секция ai. Возвращает AiConfig или None, если секции нет.

    ``context`` — строка для диагностики ошибок (обычно 'ai').

    Поля больше не обязательны (issue #230): секция только включает AI, а
    маршрутизацию/credentials ведёт Hermes. Если оператор всё ещё задал
    ``provider``/``model``/``base_url`` — предупреждаем, что они игнорируются.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    provider = _opt_str(raw, "provider", context)
    model = _opt_str(raw, "model", context)
    base_url = _opt_str(raw, "base_url", context)
    if any(v is not None for v in (provider, model, base_url)):
        logger.warning(
            "Секция 'ai' задаёт provider/model/base_url — они устарели (issue #230) "
            "и больше НЕ управляют маршрутизацией: provider/model/credentials ведёт "
            "hermes-agent-axisrow из ~/.hermes. Достаточно пустой секции 'ai' для "
            "включения AI-функциональности."
        )
    return AiConfig(provider=provider, model=model, base_url=base_url)


# ai — корневая секция (как account), не resume-подсекция, поэтому в resume-реестр
# не регистрируется; используется напрямую из load_config.

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
``provider``/``model``/``base_url`` устарели (issue #230) и НЕ управляют
маршрутизацией — при их задании парсер падает fail-closed, чтобы оператор
явно мигрировал, а не молча отправлял промпты через неожиданный маршрут.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ConfigError


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
    answer_questions: bool = False


_LEGACY_ROUTING_FIELDS = ("provider", "model", "base_url")


def parse_ai(raw, context: str) -> AiConfig | None:
    """raw — корневая секция ai. Возвращает AiConfig или None, если секции нет.

    ``context`` — строка для диагностики ошибок (обычно 'ai').

    Поля устарели (issue #230): маршрутизацию/credentials ведёт Hermes, секция
    лишь включает AI. Если оператор всё ещё задал ``provider``/``model``/
    ``base_url`` — падаем fail-closed с инструкцией по миграции (иначе молча
    ушёл бы трафик через неожиданный провайдер/аккаунт).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    # Режем по ПРИСУТСТВИЮ ключа, а не значению: `provider:` / `provider: null`
    # (пустое/нулевое значение в YAML) — тоже попытка задать устаревшую маршрутизацию,
    # её нельзя пропустить молча. api_key не входит в legacy-поля и здесь не режется.
    legacy = [k for k in _LEGACY_ROUTING_FIELDS if k in raw]
    if legacy:
        names = ", ".join(f"ai.{k}" for k in legacy)
        raise ConfigError(
            f"Поля {names} устарели (issue #230) и больше НЕ управляют маршрутизацией: "
            "provider/model/credentials ведёт hermes-agent-axisrow из ~/.hermes. "
            "Удалите эти поля и оставьте пустую секцию 'ai' (или 'ai: {}'), чтобы "
            "включить AI-функциональность."
        )
    # После fail-closed guard'а legacy-поля не могут быть не-None — AiConfig
    # всегда строится без них (маршрутизация Hermes-owned).
    answer_questions = raw.get("answer_questions", False)
    if not isinstance(answer_questions, bool):
        raise ConfigError(f"Поле '{context}.answer_questions' должно быть boolean")
    return AiConfig(answer_questions=answer_questions)


# ai — корневая секция (как account), не resume-подсекция, поэтому в resume-реестр
# не регистрируется; используется напрямую из load_config.

"""Парсер TOP-LEVEL секции questionnaires → QuestionnairesConfig (issue #482).

Как секция ``ai`` (см. ``config_sections/ai.py``) — на уровне account/throttle,
одна на весь бот, не per-resume. Парсится напрямую в ``load_config``, а НЕ
через resume-реестр ``config_sections/_registry`` (тот реестр — только для
resume-подсекций вроде ``search``/``scoring``/``ai_profile``).

Секция опциональна: при отсутствии ``load_config`` оставит
``AppConfig.questionnaires = None`` (обратная совместимость — keyword resolver
не подключается, apply ведёт себя как раньше).

``enabled`` включает keyword resolver (шаблоны + подтверждённые
сопоставления) НЕЗАВИСИМО от ``ai.answer_questions`` (issue #482: "Keyword
resolver работает без AI-зависимости") — LLM используется только как
fallback для contextual-шаблонов и не подтверждённых keyword-сопоставлений,
если ``ai.answer_questions`` тоже включена.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ConfigError


@dataclass(frozen=True)
class QuestionnairesConfig:
    """Конфигурация keyword resolver'а анкет.

    llm_match_threshold: порог уверенности, с которым LLM может ПРЕДЛОЖИТЬ
    существующий шаблон для нового текста вопроса (первое такое сопоставление
    всё равно требует подтверждения пользователя — порог только фильтрует,
    что вообще предлагается).
    llm_answer_threshold: порог уверенности для LLM-генерации ответа по
    contextual-шаблону.
    """

    enabled: bool = False
    llm_match_threshold: float = 0.90
    llm_answer_threshold: float = 0.90


def _require_unit_float(raw: dict, key: str, context: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"Поле '{context}.{key}' должно быть числом от 0 до 1")
    value = float(value)
    if not (0.0 <= value <= 1.0):
        raise ConfigError(
            f"Поле '{context}.{key}' должно быть в диапазоне [0, 1], получено: {value}"
        )
    return value


def parse_questionnaires(raw, context: str) -> QuestionnairesConfig | None:
    """raw — корневая секция questionnaires. Возвращает None, если секции нет."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"Поле '{context}.enabled' должно быть boolean")

    return QuestionnairesConfig(
        enabled=enabled,
        llm_match_threshold=_require_unit_float(
            raw, "llm_match_threshold", context, QuestionnairesConfig.llm_match_threshold
        ),
        llm_answer_threshold=_require_unit_float(
            raw, "llm_answer_threshold", context, QuestionnairesConfig.llm_answer_threshold
        ),
    )


# questionnaires — корневая секция (как ai/account), не resume-подсекция,
# поэтому в resume-реестр не регистрируется; используется напрямую из load_config.

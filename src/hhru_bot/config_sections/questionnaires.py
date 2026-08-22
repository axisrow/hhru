"""Parser for the top-level questionnaire answer automation settings."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ConfigError


@dataclass(frozen=True)
class QuestionnaireConfig:
    enabled: bool = False
    llm_match_threshold: float = 0.90
    llm_answer_threshold: float = 0.90


def _confidence(raw: dict, key: str, context: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"Поле '{context}.{key}' должно быть числом от 0 до 1")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ConfigError(f"Поле '{context}.{key}' должно быть числом от 0 до 1")
    return result


def parse_questionnaires(raw, context: str) -> QuestionnaireConfig:
    if raw is None:
        return QuestionnaireConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"Поле '{context}.enabled' должно быть boolean")
    return QuestionnaireConfig(
        enabled=enabled,
        llm_match_threshold=_confidence(raw, "llm_match_threshold", context, 0.90),
        llm_answer_threshold=_confidence(raw, "llm_answer_threshold", context, 0.90),
    )

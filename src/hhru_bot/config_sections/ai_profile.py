"""Парсер опциональной resume-секции ai_profile -> AIProfile (issue #17).

Профиль кандидата для AI-генерации сопроводительных писем: чем подробнее
заполнен, тем релевантнее письмо под вакансию. Секция опциональна: при
отсутствии load_config оставит ResumeConfig.ai_profile = None (обратная
совместимость — apply использует статичный шаблон, AI не включается).

Поля:
  - summary / profile: короткое описание себя (строка).
  - skills: список ключевых навыков.
  - highlights: список достижений/ярких фактов.
  - desired_role: желаемая роль (строка).
  - tone: тон письма ('formal' | 'friendly'), по умолчанию 'formal'.
  - cover_letter_examples: список прошлых писем как образцы стиля (#96) —
    подаются в промпт AICoverLetterProvider как few-shot. Пусто (по умолчанию)
    = без few-shot, поведение #17 не меняется. Поддерживают рандомизацию
    {a|b|c} (#86) — применяются к каждому примеру.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ConfigError
from ._registry import register

# Допустимые значения тона. Неизвестное -> ConfigError (явный контракт), а не
# молчаливый пропуск: пользователь мог опечататься и получить не тот тон.
_VALID_TONES = ("formal", "friendly")


@dataclass
class AIProfile:
    """Профиль кандидата для AI-генерации писем. Все поля опциональны."""

    summary: str = ""
    skills: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    desired_role: str = ""
    tone: str = "formal"
    cover_letter_examples: list[str] = field(default_factory=list)


def _require_str(raw, key: str, context: str) -> str:
    value = raw.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"Поле '{key}' в '{context}' должно быть строкой, получено: {value!r}")
    return value


def _require_str_list(raw, key: str, context: str) -> list[str]:
    value = raw.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"Поле '{key}' в '{context}' должно быть списком строк")
    return value


@register("ai_profile")
def parse_ai_profile(raw, context: str) -> AIProfile | None:
    """raw — подсекция ai_profile (может быть None/отсутствовать)."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")

    tone = _require_str(raw, "tone", context).strip() or "formal"
    if tone not in _VALID_TONES:
        raise ConfigError(
            f"Поле 'tone' в '{context}' должно быть одним из {_VALID_TONES}, получено: {tone!r}"
        )

    return AIProfile(
        summary=_require_str(raw, "summary", context),
        skills=_require_str_list(raw, "skills", context),
        highlights=_require_str_list(raw, "highlights", context),
        desired_role=_require_str(raw, "desired_role", context),
        tone=tone,
        cover_letter_examples=_require_str_list(raw, "cover_letter_examples", context),
    )

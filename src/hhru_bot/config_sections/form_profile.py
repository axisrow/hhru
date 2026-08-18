"""Опциональные ответы профиля для внешних анкет (#276)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ConfigError
from ._registry import register


@dataclass(frozen=True)
class FormProfile:
    """Явно заданные ответы; ключи сопоставляются с текстом вопроса."""

    answers: dict[str, str] = field(default_factory=dict)


@register("form_profile")
def parse_form_profile(raw, context: str) -> FormProfile | None:
    if not raw:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("answers", {}), dict):
        raise ConfigError(f"Секция '{context}' должна содержать answers: отображение")
    answers = raw.get("answers", {})
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in answers.items()):
        raise ConfigError(f"Все ключи и значения '{context}.answers' должны быть строками")
    return FormProfile(answers=dict(answers))

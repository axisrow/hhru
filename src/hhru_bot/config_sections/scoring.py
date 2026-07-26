"""Парсер опциональной resume-секции scoring → ScoringConfig (issue #15).

Веса факторов ранжирования вакансий. Секция опциональна: при отсутствии
load_config оставит ResumeConfig.scoring = None (обратная совместимость —
rank_candidates тогда использует нейтральные дефолтные веса).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ConfigError
from ._registry import register


@dataclass(frozen=True)
class ScoringWeights:
    """Веса факторов. Положительные — буст, отрицательные — штраф."""

    must_have: float = 2.0
    nice_to_have: float = 1.0
    exclude_keyword: float = -3.0
    text_match: float = 1.0


@dataclass(frozen=True)
class ScoringConfig:
    weights: ScoringWeights = ScoringWeights()


def _parse_weights(raw, context: str) -> ScoringWeights:
    if raw is None:
        return ScoringWeights()
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением weights: ...")
    # Каждый вес опционален; неизвестные ключи игнорируем ради обратной совместимости.
    return ScoringWeights(
        must_have=float(raw.get("must_have", ScoringWeights.must_have)),
        nice_to_have=float(raw.get("nice_to_have", ScoringWeights.nice_to_have)),
        exclude_keyword=float(raw.get("exclude_keyword", ScoringWeights.exclude_keyword)),
        text_match=float(raw.get("text_match", ScoringWeights.text_match)),
    )


@register("scoring")
def parse_scoring(raw, context: str) -> ScoringConfig | None:
    """raw — подсекция scoring (может быть None/отсутствовать)."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    return ScoringConfig(weights=_parse_weights(raw.get("weights"), f"{context}.weights"))

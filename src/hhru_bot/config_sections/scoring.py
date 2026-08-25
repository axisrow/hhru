"""Парсер опциональной resume-секции scoring → ScoringConfig (issue #15).

Веса факторов ранжирования вакансий. Секция опциональна: при отсутствии
load_config оставит ResumeConfig.scoring = None (обратная совместимость —
rank_candidates тогда использует нейтральные дефолтные веса).

Расширено issue #85: подсекция ``prefilter`` — пороги эвристического
pre-LLM фильтра работодателя (отсев мусора ДО LLM-скоринга, 0 токенов).
``prefilter`` опционален и по умолчанию ОТКЛЮЧЕН (полная обратная
совместимость: без секции pre-фильтр не применяется — поведение не меняется).
Включается флагом ``enabled: true``; пороги rating_min/reviews_min — мягкие
дефолты, перевешиваемые конфигом (см. PrefilterConfig).
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


# Дефолтные пороги pre-LLM фильтра работодателя (#85). Мягкие: отсекают только
# «слепые отклики в пустоту» (неизвестная компания без рейтинга/отзывов и без
# былого взаимодействия), не трогая всё, что выше. Десятки отзывов на hh.ru —
# уже заметный работодатель; рейтинг 3.5 — средний+ (из 5), ниже — рискованно.
_PREFILTER_DEFAULT_RATING_MIN = 3.5
_PREFILTER_DEFAULT_REVIEWS_MIN = 10


@dataclass(frozen=True)
class PrefilterConfig:
    """Пороги эвристического pre-LLM фильтра работодателя (#85).

    ``enabled`` — флаг включения фильтра (по умолчанию False: opt-in, обратная
    совместимость). ``rating_min`` — минимальный рейтинг (0-5) работодателя,
    чтобы пройти фильтр «по рейтингу». ``reviews_min`` — минимальное число
    отзывов. Карточка проходит pre-фильтр, если выполняется ЛЮБОЕ из: известная
    компания (top_tech/big_corp), rating>=rating_min, reviews_count>=reviews_min,
    работодатель раньше приглашал/смотрел (history.employer_interacted). Иначе
    отсекается как «low employer signal». trusted-бейдж hh.ru НЕ учитывается —
    им помечено ~98% карточек поиска (#118), сигнал бесполезен.
    """

    enabled: bool = False
    rating_min: float = _PREFILTER_DEFAULT_RATING_MIN
    reviews_min: int = _PREFILTER_DEFAULT_REVIEWS_MIN


@dataclass(frozen=True)
class ScoringConfig:
    weights: ScoringWeights = ScoringWeights()
    prefilter: PrefilterConfig | None = None
    resume_match_threshold: float | None = None
    letter_match_threshold: float | None = None


def _parse_weights(raw, context: str) -> ScoringWeights:
    if raw is None:
        return ScoringWeights()
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением weights: ...")
    # Каждый вес опционален; неизвестные ключи игнорируем ради обратной совместимости.
    # Не-числовое значение → ConfigError (консистентно с _require в config.py),
    # а не «голый» ValueError из float().
    weights: dict[str, float] = {}
    for key, default in (
        ("must_have", ScoringWeights.must_have),
        ("nice_to_have", ScoringWeights.nice_to_have),
        ("exclude_keyword", ScoringWeights.exclude_keyword),
        ("text_match", ScoringWeights.text_match),
    ):
        value = raw.get(key, default)
        try:
            weights[key] = float(value)
        except (TypeError, ValueError) as e:
            raise ConfigError(
                f"Вес '{key}' в '{context}' должен быть числом, получено: {value!r}"
            ) from e
    return ScoringWeights(**weights)


def _parse_prefilter(raw, context: str) -> PrefilterConfig | None:
    """raw — подсекция prefilter (может быть None/отсутствовать → None).

    None/пусто → фильтр отключен (обратная совместимость). ``enabled: true``
    включает фильтр; пороги опциональны с мягкими дефолтами PrefilterConfig.
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")

    enabled = bool(raw.get("enabled", False))

    rating_min = raw.get("rating_min", _PREFILTER_DEFAULT_RATING_MIN)
    try:
        rating_min = float(rating_min)
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"Порог 'rating_min' в '{context}' должен быть числом, получено: {rating_min!r}"
        ) from e

    reviews_min = raw.get("reviews_min", _PREFILTER_DEFAULT_REVIEWS_MIN)
    try:
        reviews_min = int(reviews_min)
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"Порог 'reviews_min' в '{context}' должен быть целым числом, получено: {reviews_min!r}"
        ) from e

    return PrefilterConfig(enabled=enabled, rating_min=rating_min, reviews_min=reviews_min)


@register("scoring")
def parse_scoring(raw, context: str) -> ScoringConfig | None:
    """raw — подсекция scoring (может быть None/отсутствовать)."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"Секция '{context}' должна быть отображением")
    return ScoringConfig(
        weights=_parse_weights(raw.get("weights"), f"{context}.weights"),
        prefilter=_parse_prefilter(raw.get("prefilter"), f"{context}.prefilter"),
        resume_match_threshold=_parse_threshold(raw.get("resume_match_threshold"), context),
        letter_match_threshold=_parse_threshold(raw.get("letter_match_threshold"), context),
    )


def _parse_threshold(value, context: str) -> float | None:
    if value is None:
        return None
    try:
        threshold = float(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Порог match-score в '{context}' должен быть числом") from e
    if not 0.0 <= threshold <= 100.0:
        raise ConfigError(f"Порог match-score в '{context}' должен быть от 0 до 100")
    return threshold or None

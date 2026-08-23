"""Cheap keyword match between an ``AIProfile`` and vacancy text (#492).

Stage 1 is deliberately observational: this module computes a 0--100 metric,
but does not decide whether a vacancy may be applied to.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .types import ScoreOutcome

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile
    from ..search import VacancyCard


_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)

# Function words add overlap without saying anything about candidate fit.  Keep
# this intentionally small and language-local rather than growing a linguistic
# dependency for the tier-0 metric.
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
    "ваш",
    "ваша",
    "ваше",
    "ваши",
    "для",
    "его",
    "или",
    "как",
    "мы",
    "на",
    "наш",
    "не",
    "но",
    "от",
    "по",
    "с",
    "со",
    "у",
    "что",
    "это",
}

# Lightweight Russian inflection folding.  It intentionally handles endings,
# not semantics: ``зарплату`` and ``зарплатные`` share a topic, while the other
# words in their phrases still keep intentually different texts far apart.
_RU_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ого",
    "ему",
    "ому",
    "ные",
    "ная",
    "ный",
    "ную",
    "ов",
    "ев",
    "ам",
    "ям",
    "ах",
    "ях",
    "ы",
    "и",
    "а",
    "я",
    "у",
    "ю",
    "е",
    "о",
)

_SECTION_WEIGHTS = {
    "desired_role": 35.0,
    "skills": 45.0,
    "summary": 10.0,
    "highlights": 10.0,
}


def _fold_token(token: str) -> str:
    token = token.casefold().replace("ё", "е")
    if not any("а" <= char <= "я" for char in token):
        return token
    for suffix in _RU_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _tokens(text: str) -> set[str]:
    return {
        folded
        for raw in _TOKEN_RE.findall(text)
        if raw.casefold().replace("ё", "е") not in _STOPWORDS
        if len(folded := _fold_token(raw)) >= 2
    }


def score_resume_match(profile: AIProfile | None, card: VacancyCard) -> ScoreOutcome:
    """Return profile-token coverage by ``card.vacancy_text`` on a 0--100 scale.

    Each populated profile section contributes its configured weight; weights
    are normalized over populated sections so a complete match is always 100.
    Coverage is measured against profile tokens (extra vacancy prose is not a
    penalty).  Multi-word evidence is important: sharing only a broad topic,
    for example ``зарплата``, cannot make a differently-intended phrase a high
    match by itself.
    """
    vacancy_tokens = _tokens(card.vacancy_text or "")
    if profile is None:
        return ScoreOutcome(score_0_100=0.0, mode="resume_match")

    sections = {
        "desired_role": _tokens(profile.desired_role),
        "skills": _tokens(" ".join(profile.skills)),
        "summary": _tokens(profile.summary),
        "highlights": _tokens(" ".join(profile.highlights)),
    }
    active = {name: tokens for name, tokens in sections.items() if tokens}
    active_weight = sum(_SECTION_WEIGHTS[name] for name in active)
    if not active or not vacancy_tokens:
        return ScoreOutcome(score_0_100=0.0, mode="resume_match")

    breakdown: dict[str, float] = {}
    for name, profile_tokens in active.items():
        coverage = len(profile_tokens & vacancy_tokens) / len(profile_tokens)
        breakdown[name] = 100.0 * _SECTION_WEIGHTS[name] / active_weight * coverage

    score = min(100.0, sum(breakdown.values()))
    return ScoreOutcome(score_0_100=score, mode="resume_match", breakdown=breakdown)

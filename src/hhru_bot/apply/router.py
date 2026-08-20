"""Account-wide vacancy routing across configured resume variants.

The router is deliberately pure: searching and submitting remain the existing
per-resume pipeline.  It only merges search results and assigns each vacancy to
one eligible resume before that pipeline is planned.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..config import ResumeConfig, is_resume_url_placeholder
from ..config_sections.scoring import ScoringConfig, ScoringWeights
from ..scoring import _normalize_heuristic_score
from ..search import VacancyCard, filter_candidates, rank_candidates

if TYPE_CHECKING:
    from ..history import History


@dataclass(frozen=True)
class MergedVacancy:
    """A vacancy observed in one or more resume search feeds."""

    card: VacancyCard
    source_resume_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResumeSelection:
    """The selected resume and the evidence used to select it."""

    resume: ResumeConfig | None
    score: float | None
    reason: str
    breakdown: dict[str, float]


def merge_vacancies(
    cards_by_resume: Iterable[tuple[ResumeConfig, Iterable[VacancyCard]]],
) -> list[MergedVacancy]:
    """Deduplicate vacancies account-wide, preserving first-feed order.

    A source feed is only a hint.  The returned source ids are diagnostic and
    never decide which resume is used.
    """

    merged: dict[str, tuple[VacancyCard, list[str]]] = {}
    for resume, cards in cards_by_resume:
        for card in cards:
            entry = merged.get(card.vacancy_id)
            if entry is None:
                merged[card.vacancy_id] = (card, [resume.resume_id])
            elif resume.resume_id not in entry[1]:
                entry[1].append(resume.resume_id)
    return [MergedVacancy(card, tuple(sources)) for card, sources in merged.values()]


def _resume_identity_is_confirmed(resume: ResumeConfig) -> bool:
    """Reject identities that cannot be safely attributed to an application."""

    return bool(
        resume.id
        and resume.resume_id
        and resume.resume_url.startswith("https://hh.ru/resume/")
        and not is_resume_url_placeholder(resume.resume_url)
    )


def _selection_reason(resume: ResumeConfig, score: float, breakdown: dict[str, float]) -> str:
    factors = [name for name, value in breakdown.items() if value]
    detail = ", ".join(factors) if factors else "profile score"
    return f"selected resume {resume.id}: score={score:g} ({detail})"


def route_vacancies(
    merged: Iterable[MergedVacancy],
    resumes: Iterable[ResumeConfig],
    history: History,
    *,
    scoring_providers: dict[str, object] | None = None,
    max_scoring_calls: int = 10,
) -> dict[str, ResumeSelection]:
    """Select at most one eligible resume for every vacancy.

    Filtering is still performed separately for every resume, preserving each
    resume's stop lists and history.  Scoring is bounded globally: once the
    budget is exhausted, remaining profiles use the existing deterministic
    heuristic.  Invalid resume identities are never eligible (fail closed).
    """

    resume_list = list(resumes)
    providers = scoring_providers or {}
    remaining = max(0, max_scoring_calls)
    routed: dict[str, ResumeSelection] = {}

    for item in merged:
        if any(
            _resume_identity_is_confirmed(resume)
            and history.has_applied(resume.resume_id, item.card.vacancy_id)
            for resume in resume_list
        ):
            # Application history is account-wide for routing.  Do not move a
            # previously submitted vacancy to another resume variant.
            continue
        best: ResumeSelection | None = None
        for resume in resume_list:
            if not _resume_identity_is_confirmed(resume):
                continue
            # A card found through another variant's search URL has not been
            # proven to satisfy this variant's positive search constraints
            # (area/salary/experience/schedule/text).  Do not route it across
            # that boundary; source feeds remain a soft hint only when no
            # positive constraints need validation.
            has_positive_constraints = any(
                (
                    resume.search.text,
                    resume.search.area is not None,
                    resume.search.salary_from is not None,
                    resume.search.experience is not None,
                    resume.search.schedule is not None,
                )
            )
            if has_positive_constraints and resume.resume_id not in item.source_resume_ids:
                continue
            candidates, _skipped = filter_candidates(
                [item.card],
                resume.search,
                resume.resume_id,
                history,
                getattr(getattr(resume, "scoring", None), "prefilter", None),
            )
            if not candidates:
                continue

            provider = providers.get(resume.id)
            # rank_candidates' shortlist is the existing cost-control boundary.
            # Use only the budget still available, then account for calls made by
            # providers that expose a simple call counter in tests/diagnostics.
            shortlist = min(remaining, 1) if provider is not None else None
            # Cross-profile routing needs a comparable score even for legacy
            # resumes that do not opt into the apply ranking section.  The
            # normal per-resume pipeline keeps its neutral legacy behaviour;
            # only this comparison uses the cheap profile heuristic.
            scoring_resume = (
                resume
                if getattr(resume, "scoring", None) is not None
                else replace(resume, scoring=ScoringConfig(weights=ScoringWeights()))
            )
            ranked = rank_candidates(
                candidates,
                resume.search,
                scoring_resume,
                scoring_provider=provider if shortlist else None,
                llm_shortlist=shortlist,
            )
            card, score, breakdown = ranked[0]
            if provider is not None and shortlist:
                remaining -= 1
            elif provider is None:
                score = _normalize_heuristic_score(score)
            selection = ResumeSelection(
                resume=resume,
                score=score,
                reason=_selection_reason(resume, score, breakdown),
                breakdown=dict(breakdown),
            )
            if best is None or score > best.score:  # stable config-order tie break
                best = selection
        if best is not None:
            routed[item.card.vacancy_id] = best
    return routed

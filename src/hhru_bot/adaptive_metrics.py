"""Read-only quality metrics for the adaptive resume pool (#947)."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from types import SimpleNamespace

from .resume_clusters import CLUSTERS_BY_KEY
from .scoring import resume_match_score
from .scoring.resume_match import NO_DATA_RATIONALE


@dataclass(frozen=True)
class ResumeMetric:
    resume_id: str
    label: str
    cluster: str | None
    samples: int
    median_score: float | None
    wins: int = 0
    comparisons: int = 0
    applies: int = 0
    successful_applies: int = 0
    invitations: int = 0
    views: int = 0

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.comparisons if self.comparisons else None


def _profile(resume):
    profile = getattr(resume, "ai_profile", None)
    if profile is not None:
        return profile
    facts = getattr(resume, "candidate_facts", None)
    if facts is None:
        return None
    skills: list[str] = []
    highlights: list[str] = []
    for collection in (facts.work_experience, facts.projects):
        for fact in collection:
            skills.extend(getattr(fact, "skills", []))
            skills.extend(getattr(fact, "tags", []))
            highlights.extend(
                filter(None, (getattr(fact, "position", ""), getattr(fact, "description", "")))
            )
    for collection in (facts.education, facts.languages):
        for fact in collection:
            skills.extend(getattr(fact, "tags", []))
            highlights.extend(
                filter(None, (getattr(fact, "specialty", ""), getattr(fact, "name", "")))
            )
    if not skills and not highlights:
        return None
    return SimpleNamespace(skills=skills, highlights=highlights, summary="", desired_role="")


def _cluster_for(text: str):
    lowered = text.casefold()
    matches = [
        cluster
        for cluster in CLUSTERS_BY_KEY.values()
        if any(k.casefold() in lowered for k in cluster.keywords)
    ]
    return matches[0] if len(matches) == 1 else None


def build_adaptive_metrics(resumes, facts: dict[str, list[dict]]) -> list[ResumeMetric]:
    """Calculate per-resume scores and outcomes from already stored observations.

    A vacancy is counted once (the newest ``vacancies_seen`` row wins). Scores
    with ``NO_DATA_RATIONALE`` are excluded, rather than being reported as real
    zeroes. A pool member is compared with the universal resume only on
    vacancies classified into that member's own fixed cluster.
    """
    vacancies = {}
    for row in facts.get("vacancies", []):
        vacancies.setdefault(str(row["vacancy_id"]), row)
    actions = facts.get("actions", [])
    responses = facts.get("responses", [])
    views = facts.get("views", [])
    universal = next((r for r in resumes if getattr(r, "cluster", None) is None), None)
    profiles = {r.resume_id: _profile(r) for r in resumes}
    outcomes = {}
    for resume in resumes:
        scores = []
        for row in vacancies.values():
            cluster = _cluster_for(row.get("vacancy_text") or row.get("title") or "")
            if getattr(resume, "cluster", None) and (
                cluster is None or cluster.key != resume.cluster
            ):
                continue
            if not getattr(resume, "cluster", None) and cluster is None:
                continue
            card = SimpleNamespace(vacancy_text=row.get("vacancy_text") or "")
            outcome = resume_match_score(card, profiles[resume.resume_id])
            if outcome.rationale != NO_DATA_RATIONALE:
                scores.append((str(row["vacancy_id"]), outcome.score_0_100))
        wins = comparisons = 0
        if universal is not None and getattr(resume, "cluster", None):
            universal_scores = {}
            for row in vacancies.values():
                cluster = _cluster_for(row.get("vacancy_text") or row.get("title") or "")
                if cluster is None or cluster.key != resume.cluster:
                    continue
                outcome = resume_match_score(
                    SimpleNamespace(vacancy_text=row.get("vacancy_text") or ""),
                    profiles[universal.resume_id],
                )
                if outcome.rationale != NO_DATA_RATIONALE:
                    universal_scores[str(row["vacancy_id"])] = outcome.score_0_100
            for vacancy_id, score in scores:
                if vacancy_id in universal_scores:
                    comparisons += 1
                    wins += score > universal_scores[vacancy_id]
        own_actions = [a for a in actions if a.get("resume_id") == resume.resume_id]
        own_responses = [r for r in responses if r.get("resume_id") == resume.resume_id]
        outcomes[resume.resume_id] = ResumeMetric(
            resume_id=resume.resume_id,
            label=resume.id,
            cluster=getattr(resume, "cluster", None),
            samples=len(scores),
            median_score=median([s for _, s in scores]) if scores else None,
            wins=wins,
            comparisons=comparisons,
            applies=len(own_actions),
            successful_applies=sum(a.get("status") == "success" for a in own_actions),
            invitations=sum(
                str(r.get("status", "")).casefold() in {"invitation", "invited", "invite"}
                for r in own_responses
            ),
            views=sum(v.get("resume_id") == resume.resume_id for v in views),
        )
    return list(outcomes.values())

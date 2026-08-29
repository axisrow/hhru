"""build_pool_plan: чистая функция планирования пула резюме (#754)."""

from __future__ import annotations

import pytest

from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.resume_clusters import CLUSTERS
from hhru_bot.resume_pool import build_pool_plan

pytestmark = pytest.mark.unit


def _resume(id_: str, *, cluster: str | None = None) -> ResumeConfig:
    return ResumeConfig(
        id=id_,
        resume_url=f"https://hh.ru/resume/{id_}abc",
        search=SearchFilters(text="python"),
        cluster=cluster,
    )


def test_no_config_resumes_plans_all_clusters():
    source = _resume("backend")
    plan = build_pool_plan([source], source)

    assert plan.missing_total == len(CLUSTERS)
    assert plan.covered == ()
    assert [item.cluster.key for item in plan.items] == [c.key for c in CLUSTERS]
    assert [item.slug for item in plan.items] == [f"backend-{c.key}" for c in CLUSTERS]


def test_covered_clusters_are_excluded_from_plan():
    source = _resume("backend")
    already = _resume("backend-ai_llm", cluster=CLUSTERS[0].key)
    plan = build_pool_plan([source, already], source)

    assert plan.covered == (CLUSTERS[0],)
    assert plan.missing_total == len(CLUSTERS) - 1
    assert CLUSTERS[0].key not in [item.cluster.key for item in plan.items]


def test_fully_covered_pool_plans_nothing():
    source = _resume("backend")
    covered = [_resume(f"backend-{c.key}", cluster=c.key) for c in CLUSTERS]
    plan = build_pool_plan([source, *covered], source)

    assert plan.items == ()
    assert plan.missing_total == 0
    assert set(plan.covered) == set(CLUSTERS)


def test_limit_trims_plan_without_depending_on_cluster_count():
    source = _resume("backend")
    plan = build_pool_plan([source], source, limit=1)

    assert len(plan.items) == 1
    # missing_total stays the true count of missing clusters, independent of
    # --limit — used by the command to report "N из M" honestly.
    assert plan.missing_total == len(CLUSTERS)


def test_limit_larger_than_missing_never_creates_extra_items():
    source = _resume("backend")
    plan = build_pool_plan([source], source, limit=len(CLUSTERS) + 100)

    assert len(plan.items) == len(CLUSTERS)


def test_limit_zero_plans_nothing():
    source = _resume("backend")
    plan = build_pool_plan([source], source, limit=0)

    assert plan.items == ()
    assert plan.missing_total == len(CLUSTERS)


def test_negative_limit_is_clamped_to_zero():
    source = _resume("backend")
    plan = build_pool_plan([source], source, limit=-5)

    assert plan.items == ()

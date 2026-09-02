"""Unit tests for the read-only adaptive-pool metric (#947)."""

from types import SimpleNamespace

import pytest

from hhru_bot.adaptive_metrics import build_adaptive_metrics
from hhru_bot.report_adaptive import success_statement

pytestmark = pytest.mark.unit


def resume(name, skills, cluster=None):
    return SimpleNamespace(
        id=name,
        resume_id={"universal": "00001", "backend": "00002"}[name],
        cluster=cluster,
        ai_profile=SimpleNamespace(skills=skills, desired_role="", summary="", highlights=[]),
        candidate_facts=None,
    )


def test_pool_wins_against_universal_and_counts_read_only_facts():
    resumes = [resume("universal", ["Excel"]), resume("backend", ["Django"], "python_backend")]
    facts = {
        "vacancies": [
            {"vacancy_id": "v1", "title": "Backend", "vacancy_text": "Django REST API"},
            {"vacancy_id": "v2", "title": "Backend", "vacancy_text": "Django"},
        ],
        "actions": [{"resume_id": "00002", "vacancy_id": "v1", "status": "success"}],
        "responses": [{"resume_id": "00002", "vacancy_id": "v1", "status": "invitation"}],
        "views": [{"resume_id": "00002", "viewed_at": "2026-01-01"}],
    }

    metrics = build_adaptive_metrics(resumes, facts)

    pool = metrics[1]
    assert pool.median_score == 100.0
    assert pool.comparisons == 2
    assert pool.wins == 2
    assert pool.applies == pool.successful_applies == pool.invitations == pool.views == 1


def test_no_scores_are_reported_as_insufficient_data():
    resumes = [resume("universal", ["Excel"]), resume("backend", ["Django"], "python_backend")]
    metrics = build_adaptive_metrics(
        resumes,
        {"vacancies": [], "actions": [], "responses": [], "views": []},
    )
    assert all(metric.median_score is None for metric in metrics)
    assert all(metric.win_rate is None for metric in metrics)


def test_below_threshold_is_not_reported_as_success():
    metric = SimpleNamespace(wins=1, comparisons=4)

    statement = success_statement([metric])

    assert statement.startswith("[INFO]")
    assert "25.0%" in statement

"""Тесты фиксированного списка кластеров резюме (#752/#753)."""

from __future__ import annotations

import pytest

from hhru_bot.resume_clusters import (
    AI_LLM,
    CLUSTERS,
    DATA_ENGINEER,
    DATA_SCIENCE,
    PYTHON_BACKEND,
    cluster_by_key,
)

pytestmark = pytest.mark.unit


def test_exactly_four_clusters() -> None:
    """#752: замер дал четыре кластера, а не три из исходной гипотезы эпика."""
    assert len(CLUSTERS) == 4


def test_data_science_is_not_merged_into_ai_llm() -> None:
    """Ключевое из #752: Data Science/ML — самостоятельный кластер, не часть AI/LLM."""
    assert DATA_SCIENCE.key != AI_LLM.key
    assert set(DATA_SCIENCE.tags).isdisjoint(AI_LLM.tags)
    assert set(DATA_SCIENCE.keywords).isdisjoint(AI_LLM.keywords)


def test_devops_cluster_does_not_exist() -> None:
    """DevOps/Infra отвергнут в #752 — не должен появиться как пятый кластер."""
    keys = {c.key for c in CLUSTERS}
    assert "devops" not in keys
    assert "infra" not in keys


def test_cluster_by_key_returns_expected_cluster() -> None:
    assert cluster_by_key("data_engineer") is DATA_ENGINEER
    assert cluster_by_key("python_backend") is PYTHON_BACKEND


def test_cluster_by_key_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="неизвестный кластер"):
        cluster_by_key("devops")


def test_all_clusters_have_distinct_keys_and_nonempty_tags() -> None:
    keys = [c.key for c in CLUSTERS]
    assert len(keys) == len(set(keys))
    for cluster in CLUSTERS:
        assert cluster.tags
        assert cluster.keywords

"""Тесты resume-секции cluster (issue #754)."""

from __future__ import annotations

import textwrap

import pytest

from hhru_bot.config import ConfigError, load_config
from hhru_bot.config_sections.cluster import parse_cluster
from hhru_bot.resume_clusters import CLUSTERS

pytestmark = pytest.mark.unit


def test_parse_cluster_none_when_raw_is_none():
    assert parse_cluster(None, "resumes[0].cluster") is None


def test_parse_cluster_valid_key_returned_as_is():
    key = CLUSTERS[0].key
    assert parse_cluster(key, "resumes[0].cluster") == key


def test_parse_cluster_unknown_key_raises():
    with pytest.raises(ConfigError, match="неизвестный кластер"):
        parse_cluster("not_a_real_cluster", "resumes[0].cluster")


def test_parse_cluster_wrong_type_raises():
    with pytest.raises(ConfigError, match="должно быть строкой"):
        parse_cluster(123, "resumes[0].cluster")


def _base_config_yaml(resume_extra: str = "") -> str:
    return textwrap.dedent(
        f"""
        account:
          storage_state_file: storage_state/hh_session.json
        cover_letter_default: "..."
        resumes:
          - id: "backend"
            resume_url: "https://hh.ru/resume/deadbeefdeadbeefdeadbeefdeadbeef"
            search:
              text: "python"
            {resume_extra}
        """
    )


def test_load_config_without_cluster_section_defaults_to_none(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_base_config_yaml(), encoding="utf-8")

    config = load_config(config_path)

    assert config.resumes[0].cluster is None


def test_load_config_with_valid_cluster(tmp_path):
    key = CLUSTERS[1].key
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_base_config_yaml(f'cluster: "{key}"'), encoding="utf-8")

    config = load_config(config_path)

    assert config.resumes[0].cluster == key


def test_load_config_with_unknown_cluster_raises(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_base_config_yaml('cluster: "bogus"'), encoding="utf-8")

    with pytest.raises(ConfigError, match="неизвестный кластер"):
        load_config(config_path)

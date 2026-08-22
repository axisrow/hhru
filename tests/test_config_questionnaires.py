"""Parsing tests for the top-level `questionnaires` section (issue #482).

Mirrors the shape of `config_sections/ai.py`'s tests: `questionnaires` is a
root-level section (like `ai`/`account`), not a resume-subsection, parsed
directly by `load_config` -- NOT through `config_sections._registry`.
"""

from __future__ import annotations

import textwrap

import pytest

from hhru_bot.config import ConfigError, load_config
from hhru_bot.config_sections.questionnaires import QuestionnairesConfig, parse_questionnaires

pytestmark = pytest.mark.unit


def _write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _minimal_config(extra: str = "") -> str:
    indented_extra = textwrap.indent(extra, "        ") if extra else ""
    return f"""
        account:
          storage_state_file: data/storage_state/hh_session.json
        resumes:
          - id: r1
            resume_url: "https://hh.ru/resume/AAA111"
            search:
              text: "python developer"
{indented_extra}
    """


# --- parse_questionnaires (unit, no load_config) ----------------------------


def test_parse_questionnaires_absent_returns_none():
    assert parse_questionnaires(None, "questionnaires") is None


def test_parse_questionnaires_empty_dict_uses_defaults():
    cfg = parse_questionnaires({}, "questionnaires")
    assert cfg == QuestionnairesConfig(
        enabled=False, llm_match_threshold=0.90, llm_answer_threshold=0.90
    )


def test_parse_questionnaires_explicit_values():
    cfg = parse_questionnaires(
        {"enabled": True, "llm_match_threshold": 0.8, "llm_answer_threshold": 0.75},
        "questionnaires",
    )
    assert cfg.enabled is True
    assert cfg.llm_match_threshold == 0.8
    assert cfg.llm_answer_threshold == 0.75


def test_parse_questionnaires_not_a_mapping_raises():
    with pytest.raises(ConfigError, match="questionnaires"):
        parse_questionnaires([], "questionnaires")


def test_parse_questionnaires_enabled_wrong_type_raises():
    with pytest.raises(ConfigError, match="enabled"):
        parse_questionnaires({"enabled": "yes"}, "questionnaires")


@pytest.mark.parametrize("field", ["llm_match_threshold", "llm_answer_threshold"])
def test_parse_questionnaires_threshold_wrong_type_raises(field):
    with pytest.raises(ConfigError, match=field):
        parse_questionnaires({field: "high"}, "questionnaires")


@pytest.mark.parametrize("field", ["llm_match_threshold", "llm_answer_threshold"])
@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_parse_questionnaires_threshold_out_of_range_raises(field, value):
    with pytest.raises(ConfigError, match=field):
        parse_questionnaires({field: value}, "questionnaires")


# --- load_config integration -------------------------------------------------


def test_load_config_questionnaires_absent_is_none(tmp_path):
    path = _write_config(tmp_path, _minimal_config())
    config = load_config(path)
    assert config.questionnaires is None


def test_load_config_questionnaires_enabled(tmp_path):
    path = _write_config(
        tmp_path,
        _minimal_config("questionnaires:\n  enabled: true\n  llm_match_threshold: 0.85\n"),
    )
    config = load_config(path)
    assert config.questionnaires is not None
    assert config.questionnaires.enabled is True
    assert config.questionnaires.llm_match_threshold == 0.85
    assert config.questionnaires.llm_answer_threshold == 0.90  # default preserved

"""Pure safety and LLM-contract tests for additional resume sections."""

from __future__ import annotations

import pytest

from hhru_bot.config import ConfigError
from hhru_bot.config_sections.resume_sections import parse_resume_sections
from hhru_bot.resume_sections import parse_plan

pytestmark = pytest.mark.unit


def test_config_rejects_unconfirmed_blocks() -> None:
    with pytest.raises(ConfigError):
        parse_resume_sections({"blocks": ["certificates"]}, "resumes[0].resume_sections")


def test_plan_parses_only_requested_typed_blocks() -> None:
    plan = parse_plan(
        '{"attestations": [{"name": "AWS", "organization": "Amazon", '
        '"specialty": "Cloud", "year": "2024"}], '
        '"recommendations": [{"text": "Great", "company": "Acme"}]}',
        ["recommendations"],
    )
    assert plan.attestations == []
    assert plan.recommendations[0].company == "Acme"


def test_malformed_llm_output_is_fail_closed() -> None:
    plan = parse_plan("not json", ["attestations", "recommendations"])
    assert not plan.attestations
    assert not plan.recommendations
    assert plan.skipped

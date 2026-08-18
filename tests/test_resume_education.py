"""Unit coverage for the #262 education plan contract and safety fallback."""

import pytest

from hhru_bot.ai.types import NormalizedResponse
from hhru_bot.config_sections.education import EducationRecord, parse_education
from hhru_bot.resume_education import generate_education_plan

pytestmark = pytest.mark.unit


class LLM:
    def __init__(self, content):
        self.content = content
        self.messages = []

    def chat(self, messages, **kwargs):
        self.messages.append(messages)
        return NormalizedResponse(self.content, None, "stop")


def test_plan_supports_multiple_primary_and_separate_additional():
    llm = LLM(
        '{"primary":[{"institution":"МГУ","level":"бакалавриат",'
        '"specialty":"Физика","year":"2015"},{"institution":"НИУ ВШЭ",'
        '"level":"магистратура","specialty":"Аналитика","year":"2017"}],'
        '"additional":[{"institution":"Курс SQL","level":"",'
        '"specialty":"SQL","year":"2020"}]}'
    )
    plan = generate_education_plan(llm, "Учился в МГУ и ВШЭ; курс SQL в 2020", mode="from_scratch")
    assert len(plan.primary) == 2
    assert plan.primary[1].institution == "НИУ ВШЭ"
    assert plan.additional[0].specialty == "SQL"
    assert "Не выдумывай" in llm.messages[0][0]["content"]
    assert "faculty" in llm.messages[0][0]["content"]


def test_bad_llm_response_preserves_prefill_without_fabrication():
    current = [EducationRecord("МГУ", "", "", "2015")]
    plan = generate_education_plan(LLM("not json"), "", mode="prefill", current_primary=current)
    assert plan.used_fallback is True
    assert plan.primary == current
    assert plan.additional == []


def test_config_parses_both_blocks_and_mode():
    config = parse_education(
        {
            "source": "мой профиль",
            "mode": "prefill",
            "primary": [{"institution": "МГУ", "year": 2015}],
            "additional": [{"institution": "Курс", "specialty": "SQL"}],
        },
        "resumes[0].education",
    )
    assert config.mode == "prefill"
    assert config.primary[0].year == "2015"
    assert config.additional[0].specialty == "SQL"


def test_config_rejects_unknown_mode():
    with pytest.raises(Exception, match="from_scratch"):
        parse_education({"mode": "guess"}, "education")

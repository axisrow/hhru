import pytest

from hhru_bot.experience import (
    ExperienceEntry,
    build_prompt,
    parse_plan,
    plan_experience,
)

pytestmark = pytest.mark.unit


def test_parse_plan_accepts_multiple_entries_and_achievements():
    entries = parse_plan(
        '[{"company":"Acme","position":"Engineer","start_year":"2020",'
        '"end_year":"2022","current":false,"duties":"API",'
        '"achievements":["-20% latency"],"company_url":""},'
        '{"company":"Beta","position":"Lead","start_year":"2022",'
        '"end_year":"","current":true,"duties":"Team",'
        '"achievements":[],"company_url":""}]'
    )
    assert entries is not None
    assert len(entries) == 2
    assert entries[0].description() == "API\n\nДостижения:\n- -20% latency"
    assert entries[1].current is True


def test_parse_plan_rejects_non_json_or_partial_shape():
    assert parse_plan("не JSON") is None
    assert parse_plan('[{"company":"Acme"}]') is not None
    assert parse_plan('[{"company":3}]') is None


def test_plan_fallback_preserves_existing_without_fabrication():
    existing = [ExperienceEntry(company="Acme", duties="old")]

    class Failing:
        def chat(self, *args, **kwargs):
            raise RuntimeError("offline")

    plan = plan_experience(Failing(), mode="fill", career="facts", existing=existing)
    assert plan.used_fallback is True
    assert plan.entries == existing


def test_prompt_contains_fill_context_and_fact_guard():
    prompt = build_prompt("fill", "facts", [ExperienceEntry(company="Acme")])
    assert "только сведения пользователя" in prompt[0]["content"]
    assert '"existing"' in prompt[1]["content"]

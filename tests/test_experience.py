import pytest

from hhru_bot.experience import (
    ExperienceEntry,
    ExperiencePlan,
    ExperienceResult,
    _merge_fill_plan,
    build_prompt,
    edit_experience_on_hh,
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


def test_fill_plan_preserves_existing_fields_and_text():
    old = ExperienceEntry(company="Acme", position="Engineer", duties="existing")
    proposed = ExperienceEntry(
        company="Acme", position="Engineer", duties="new", achievements=["metric"]
    )
    merged = _merge_fill_plan([old], [proposed])
    assert merged == [
        ExperienceEntry(
            company="Acme", position="Engineer", duties="existing", achievements=["metric"]
        )
    ]


def test_fill_plan_rejects_identity_or_count_changes():
    old = ExperienceEntry(company="Acme")
    assert _merge_fill_plan([old], [ExperienceEntry(company="Other")]) is None
    assert _merge_fill_plan([old], []) is None


class _Locator:
    def count(self):
        return 0


class _Page:
    def locator(self, selector):
        assert selector is not None
        return _Locator()


def test_edit_experience_fails_closed_when_add_selector_is_unavailable(monkeypatch):
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_ADD_BUTTON", None)

    results = edit_experience_on_hh(
        _Page(),
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme")]),
        dry_run=True,
    )

    assert results == [ExperienceResult("строка опыта 0: add-триггер не подтверждён однозначно")]

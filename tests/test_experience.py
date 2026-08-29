import pytest

from hhru_bot.experience import (
    _EXPERIENCE_ADD_FALLBACK_SELECTOR,
    _EXPERIENCE_CARD_SELECTOR,
    ExperienceEntry,
    ExperiencePlan,
    ExperienceResult,
    _find_add_trigger,
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
    def __init__(self, count: int = 0):
        self._count = count
        self._clicked = False

    def count(self) -> int:
        return self._count

    @property
    def first(self) -> "_Locator":
        return self

    def click(self):
        self._clicked = True

    def wait_for(self, *, state: str = "visible", timeout: int = 0):
        return None

    def input_value(self) -> str:
        return ""

    def fill(self, value: str) -> None:
        return None

    def is_visible(self) -> bool:
        return True

    def get_attribute(self, name: str) -> str | None:
        return None


class _Page:
    def __init__(self, locators: dict[str, _Locator] | None = None):
        self._locators = locators or {}
        self._url = "https://hh.ru/resume/resume-1"

    @property
    def url(self) -> str:
        return self._url

    def locator(self, selector: str):
        if selector in self._locators:
            return self._locators[selector]
        return _Locator()

    def wait_for_url(self, url: str, *, wait_until: str = "commit", timeout: int = 0):
        return None

    def reload(self, *, wait_until: str = "domcontentloaded"):
        return None


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


def test_find_add_trigger_prefers_confirmed_over_fallback(monkeypatch):
    confirmed = _Locator(count=1)
    page = _Page(
        locators={
            "confirmed-selector": confirmed,
            _EXPERIENCE_ADD_FALLBACK_SELECTOR: _Locator(count=1),
        }
    )
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_ADD_BUTTON", "confirmed-selector")

    loc, source = _find_add_trigger(page)
    assert source == "confirmed"
    assert loc is confirmed


def test_find_add_trigger_uses_fallback_when_confirmed_is_none(monkeypatch):
    fallback = _Locator(count=1)
    page = _Page(
        locators={
            _EXPERIENCE_ADD_FALLBACK_SELECTOR: fallback,
        }
    )
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_ADD_BUTTON", None)

    loc, source = _find_add_trigger(page)
    assert source == "fallback"
    assert loc is fallback


def test_find_add_trigger_returns_none_when_no_trigger_available(monkeypatch):
    page = _Page()
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_ADD_BUTTON", None)

    loc, source = _find_add_trigger(page)
    assert loc is None
    assert source is None


def test_edit_experience_uses_fallback_add_trigger_when_confirmed_is_unavailable(monkeypatch):
    edit_button = _Locator(count=0)
    fallback_add = _Locator(count=1)
    company = _Locator(count=1)
    position = _Locator(count=1)
    start_year = _Locator(count=1)
    description = _Locator(count=1)
    save = _Locator(count=1)
    cancel = _Locator(count=1)
    experience_card = _Locator(count=1)

    def _click_fallback():
        fallback_add._clicked = True
        edit_button._count = 1

    fallback_add.click = _click_fallback

    class _PostSavePage(_Page):
        def reload(self, *, wait_until: str = "domcontentloaded"):
            experience_card._count = 2
            return None

    page = _PostSavePage(
        locators={
            "[data-qa='edit-experience-button-0']": edit_button,
            _EXPERIENCE_ADD_FALLBACK_SELECTOR: fallback_add,
            "[data-qa='resume-profile-experience-specific-company-input-0']": company,
            "[data-qa='resume-profile-experience-specific-position-input-0']": position,
            "[data-qa='resume-editor-experience-start-year-input']": start_year,
            "[data-qa='resume-editor-experience-description-input']": description,
            "[data-qa='profile-layout-save-button']": save,
            "[data-qa='profile-layout-cancel-button']": cancel,
            _EXPERIENCE_CARD_SELECTOR: experience_card,
        }
    )
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_ADD_BUTTON", None)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan(
            [ExperienceEntry(company="Acme", position="Eng", start_year="2020", duties="duties")]
        ),
        dry_run=False,
    )

    assert len(results) == 1
    assert "сохранено и привязано к резюме" in results[0].reason
    assert results[0].success is True
    assert fallback_add._clicked is True


def test_edit_experience_fails_closed_when_binding_does_not_increase_card_count(monkeypatch):
    edit_button = _Locator(count=0)
    fallback_add = _Locator(count=1)
    company = _Locator(count=1)
    position = _Locator(count=1)
    start_year = _Locator(count=1)
    description = _Locator(count=1)
    save = _Locator(count=1)
    cancel = _Locator(count=1)
    experience_card = _Locator(count=1)

    def _click_fallback():
        fallback_add._clicked = True
        edit_button._count = 1

    fallback_add.click = _click_fallback

    class _NoBindingPage(_Page):
        def reload(self, *, wait_until: str = "domcontentloaded"):
            # Simulate no binding: card count stays the same
            return None

    page = _NoBindingPage(
        locators={
            "[data-qa='edit-experience-button-0']": edit_button,
            _EXPERIENCE_ADD_FALLBACK_SELECTOR: fallback_add,
            "[data-qa='resume-profile-experience-specific-company-input-0']": company,
            "[data-qa='resume-profile-experience-specific-position-input-0']": position,
            "[data-qa='resume-editor-experience-start-year-input']": start_year,
            "[data-qa='resume-editor-experience-description-input']": description,
            "[data-qa='profile-layout-save-button']": save,
            "[data-qa='profile-layout-cancel-button']": cancel,
            _EXPERIENCE_CARD_SELECTOR: experience_card,
        }
    )
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.EXPERIENCE_ADD_BUTTON", None)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan(
            [ExperienceEntry(company="Acme", position="Eng", start_year="2020", duties="duties")]
        ),
        dry_run=False,
    )

    assert len(results) == 1
    assert "запись не привязалась к резюме" in results[0].reason
    assert results[0].success is False
    assert results[0].uncertain is False

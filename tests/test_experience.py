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
    read_experience_on_hh,
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
    def __init__(self, count=0):
        self._count = count

    def count(self):
        return self._count

    def format(self, **_kwargs):
        return self

    def wait_for(self, *, timeout=None, state=None):
        return None

    def fill(self, _value):
        return None

    def click(self):
        return None

    def input_value(self):
        return ""


class _Page:
    """First-row form is a distinct data-qa namespace (#786/#787): every
    field/button locator here is unconditionally count()==1, matching the
    live-confirmed /resume/edit/{id}/experience shape, except the row editor
    locators (edit-experience-button/EXPERIENCE_COMPANY/POSITION), which stay
    at count()==0 to force the first-entry branch.
    """

    def __init__(self):
        self.url = "https://hh.ru/resume/resume-1"

    def locator(self, selector):
        assert selector is not None
        if "edit-experience-button" in selector or "specific-" in selector:
            return _Locator(count=0)
        return _Locator(count=1)


def test_edit_experience_first_entry_uses_resume_scoped_route(monkeypatch):
    """#786/#787: an empty resume has no in-page add trigger; the first row
    is created by navigating straight to /resume/edit/{id}/experience, which
    live testing confirmed opens pre-bound to that resume_id."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    seen_urls = []

    def fake_goto(page, url):
        seen_urls.append(url)
        page.url = "https://hh.ru/resume/edit/resume-1/experience"

    monkeypatch.setattr("hhru_bot.experience.goto_hh", fake_goto)

    results = edit_experience_on_hh(
        _Page(),
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=True,
    )

    assert seen_urls == ["https://hh.ru/resume/edit/resume-1/experience"]
    assert results == [ExperienceResult("строка 0: предложено, save не нажат", True)]


def test_edit_experience_first_entry_fails_closed_on_route_mismatch(monkeypatch):
    """If hh.ru does not land on the expected resume-scoped route (drifted
    selector/redirect), fail closed instead of filling an unconfirmed form."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)

    def fake_goto(page, url):
        page.url = "https://hh.ru/profile/edit/experience"

    monkeypatch.setattr("hhru_bot.experience.goto_hh", fake_goto)

    results = edit_experience_on_hh(
        _Page(),
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme")]),
        dry_run=True,
    )

    assert len(results) == 1
    assert not results[0].success
    assert "форма открыта не для того резюме" in results[0].reason


class _SavePage:
    """Fake page for the non-dry-run save path with a mutable row count
    (#796/#787): edit-experience-button/{index} locators reflect ``rows``,
    letting a test simulate the count growing (bound save) or staying flat
    (silent no-op — save landed on the shared profile, not this resume)."""

    def __init__(self, rows: int, *, grow_on_reload_by: int = 0):
        self.url = "https://hh.ru/resume/resume-1"
        self.rows = rows
        self._grow_on_reload_by = grow_on_reload_by
        self._reloaded = False

    def locator(self, selector):
        if "edit-experience-button" in selector:
            index = int(selector.rsplit("-", 1)[-1].rstrip("]").strip("'"))
            return _Locator(count=1 if index < self.rows else 0)
        return _Locator(count=1)

    def wait_for_url(self, url, *, wait_until=None, timeout=None):
        return None

    def reload(self, *, timeout=None, wait_until=None):
        self._reloaded = True
        self.rows += self._grow_on_reload_by


def _fake_goto_to_edit_path(page, url):
    page.url = url.replace("https://hh.ru", "")


def test_edit_experience_first_entry_verifies_binding_after_reload(monkeypatch):
    """#796: a successful save is only reported once a post-save reload
    shows the row count actually grew on THIS resume — not just that the
    save click and URL landed without error."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _SavePage(rows=0, grow_on_reload_by=1)
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
    )

    assert page._reloaded is True
    assert results == [ExperienceResult("строка 0: сохранено и привязано к резюме", True)]


def test_edit_experience_first_entry_fails_closed_when_row_count_does_not_grow(monkeypatch):
    """#796/#787: save succeeded and identity matched, but the row count did
    not grow after reload — the entry silently landed elsewhere. Report
    failure rather than a false [OK]."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _SavePage(rows=0, grow_on_reload_by=0)
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
    )

    assert len(results) == 1
    assert not results[0].success
    assert not results[0].uncertain
    assert "не привязалась к резюме" in results[0].reason


def test_edit_experience_existing_row_edit_does_not_require_count_growth(monkeypatch):
    """Fill mode re-saves an EXISTING row in place (same index) — the row
    count staying flat after reload must not be flagged as a binding
    failure; that check only applies to a genuinely new first_entry row."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _SavePage(rows=1, grow_on_reload_by=0)
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
    )

    assert results == [ExperienceResult("строка 0: сохранено и привязано к резюме", True)]


class _UnreadableRowLocator(_Locator):
    def input_value(self):
        raise ValueError("поле определяется неоднозначно (0)")


class _ReadPage:
    """Two experience rows; row 0 is confirmed unreadable (drifted field),
    row 1 reads normally — #796's resilience fix must skip row 0, not fail
    the whole read."""

    def locator(self, selector):
        if "edit-experience-button" in selector:
            index = int(selector.rsplit("-", 1)[-1].rstrip("]").strip("'"))
            return _Locator(count=1 if index < 2 else 0)
        if "specific-company-input-0" in selector:
            return _UnreadableRowLocator(count=1)
        return _Locator(count=1)


def test_read_experience_skips_unreadable_row_instead_of_failing_whole_read(monkeypatch):
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)

    result = read_experience_on_hh(_ReadPage(), "resume-1")

    assert len(result) == 1

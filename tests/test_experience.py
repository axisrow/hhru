import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot.experience import (
    MONTH_NAMES,
    ExperienceEntry,
    ExperiencePlan,
    ExperienceResult,
    _experience_row_indexes,
    _merge_fill_plan,
    build_prompt,
    edit_experience_on_hh,
    parse_plan,
    plan_experience,
    read_experience_on_hh,
)
from hhru_bot.selector_groups.resume_experience import (
    EXPERIENCE_COMPANY,
    EXPERIENCE_MONTH_LISTBOX,
    EXPERIENCE_MONTH_OPTION,
    EXPERIENCE_POSITION,
    FIRST_EXPERIENCE_CURRENT_CHECKBOX,
    SHARED_EXPERIENCE_CANCEL,
    SHARED_EXPERIENCE_SAVE,
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


def test_plan_create_falls_back_when_llm_omits_start_month():
    """PR #818 review: build_prompt() asks the LLM for start_month, but
    nothing enforced it before this fix — a plan with company/position but a
    blank start_month would reach edit_experience_on_hh and only fail once
    hh.ru itself rejects the save (unclear uncertain/failed), instead of a
    clear reason up front like the CLI --entry path already gives."""

    class Returning:
        def __init__(self, content):
            self._content = content

        def chat(self, *args, **kwargs):
            return type("R", (), {"content": self._content})()

    content = (
        '[{"company":"Acme","position":"Engineer","start_year":"2020",'
        '"start_month":"","end_year":"","current":true,"duties":"API",'
        '"achievements":[],"company_url":""}]'
    )
    plan = plan_experience(Returning(content), mode="create", career="facts", existing=None)
    assert plan.used_fallback is True
    assert "start_month" in plan.reason


def test_plan_fill_falls_back_when_existing_start_month_is_blank():
    """Same guard applies in fill mode: _merge_fill_plan only protects
    start_month from being *changed*, not from being blank on both sides
    (e.g. rows read from hh.ru before this fix had no start_month at all)."""
    existing = [
        ExperienceEntry(company="Acme", position="Engineer", start_year="2020", current=True)
    ]

    class Returning:
        def chat(self, *args, **kwargs):
            content = (
                '[{"company":"Acme","position":"Engineer","start_year":"2020",'
                '"start_month":"","end_year":"","current":true,"duties":"new",'
                '"achievements":[],"company_url":""}]'
            )
            return type("R", (), {"content": content})()

    plan = plan_experience(Returning(), mode="fill", career="facts", existing=existing)
    assert plan.used_fallback is True
    assert "start_month" in plan.reason


def test_plan_accepts_llm_entry_with_start_month():
    class Returning:
        def chat(self, *args, **kwargs):
            content = (
                '[{"company":"Acme","position":"Engineer","start_year":"2020",'
                '"start_month":"3","end_year":"","current":true,"duties":"API",'
                '"achievements":[],"company_url":""}]'
            )
            return type("R", (), {"content": content})()

    plan = plan_experience(Returning(), mode="create", career="facts", existing=None)
    assert plan.used_fallback is False
    assert plan.entries[0].start_month == "3"


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
    def __init__(self, count=0, *, enabled=True, text="Месяц", checked=False):
        self._count = count
        self._enabled = enabled
        self._text = text
        self._checked = checked

    def count(self):
        return self._count

    def format(self, **_kwargs):
        return self

    def wait_for(self, *, timeout=None, state=None):
        return None

    def fill(self, value):
        # #956: _fill_stable verifies through input_value(), so the fake
        # remembers what it was filled with (like the real controlled input).
        self._filled_value = value

    def click(self, *, timeout=None, **_kwargs):
        return None

    def scroll_into_view_if_needed(self, *, timeout=None, **_kwargs):
        return None

    def input_value(self):
        return getattr(self, "_filled_value", "")

    def inner_text(self):
        return self._text

    def is_enabled(self):
        return self._enabled

    def is_checked(self):
        return self._checked

    def element_handle(self, *, timeout=None):
        return object()

    @property
    def first(self):
        return self


class _RowButtonsLocator(_Locator):
    """Fake for EXPERIENCE_EDIT_BUTTONS_ALL (#815): a group locator over a
    non-contiguous set of row indexes, supporting the .nth()/.get_attribute()
    pair `_experience_row_indexes()` uses to enumerate real indexes instead
    of assuming a 0..N-1 range."""

    def __init__(self, indexes):
        super().__init__(count=len(indexes))
        self._indexes = list(indexes)

    def nth(self, i):
        index = self._indexes[i]
        return _RowButtonLocator(index)


class _RowButtonLocator(_Locator):
    def __init__(self, index):
        super().__init__(count=1)
        self._index = index

    def get_attribute(self, name):
        assert name == "data-qa"
        return f"edit-experience-button-{self._index}"


class _Page:
    """First-row form is a distinct data-qa namespace (#786/#787): every
    field/button locator here is unconditionally count()==1, matching the
    live-confirmed /resume/edit/{id}/experience shape, except the row editor
    locators (edit-experience-button/EXPERIENCE_COMPANY/POSITION), which stay
    at count()==0 to force the first-entry branch (resume has zero rows).
    """

    def __init__(self):
        self.url = "https://hh.ru/resume/resume-1"

    def wait_for_timeout(self, _ms):
        return None

    def locator(self, selector):
        assert selector is not None
        if selector.startswith("[data-qa^='edit-experience-button-']"):
            return _RowButtonsLocator([])
        if "Развернуть" in selector:
            return _Locator(count=0)
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


class _PanelCheckbox(_Locator):
    """Fake for scope.get_by_role("checkbox", name=title, exact=True)
    (#782 PR review: no longer a hand-built CSS attribute selector). click()
    actually toggles ``_checked`` (base _Locator.click() is a no-op), so a
    test can assert reconciliation logic really changed the panel state."""

    def click(self, *, timeout=None, **_kwargs):
        self._checked = not self._checked


class _PanelScopeLocator(_Locator):
    """Fake for the "Резюме с этим местом работы" panel container returned
    by ``page.locator(EXPERIENCE_RESUME_PANEL_SCOPE)``. #782 PR review:
    ``_reconcile_experience_resume_panel`` resolves each checkbox via
    ``scope.get_by_role("checkbox", name=title, exact=True)`` instead of a
    hand-built CSS selector — this fake mirrors that by dispatching to a
    ``checkboxes`` dict keyed by title (a resume's accessible name), the
    same lookup Playwright's role locator performs internally."""

    def __init__(
        self,
        checkboxes: dict[str, _PanelCheckbox],
        *,
        count=1,
        visible_count: int | None = None,
        is_expanded=None,
    ):
        super().__init__(count=count)
        self._checkboxes = checkboxes
        self._visible_count = visible_count
        self._is_expanded = is_expanded

    def get_by_role(self, role, *, name=None, exact=False):
        assert role == "checkbox"
        if name is None:
            count = len(self._checkboxes)
            if self._visible_count is not None and not self._is_expanded():
                count = self._visible_count
            return _Locator(count=count)
        assert exact is True
        return self._checkboxes.get(name, _Locator(count=0))


class _SavePage:
    """Fake page for the non-dry-run save path with a mutable set of row
    indexes (#796/#787/#815): edit-experience-button locators reflect
    ``indexes``, letting a test simulate a new index appearing after reload
    (bound save) or the set staying flat (silent no-op — save landed on the
    shared profile, not this resume). Indexes are deliberately non-
    contiguous-friendly (any int, not just 0..N-1) to match #815's live
    finding that hh.ru's row index is an internal counter, not a position.

    #782: also models the "Резюме с этим местом работы" binding panel.
    ``panel_titles`` maps each account resume's title to its initial checked
    state; omitted/None disables the panel entirely (scope count()==0),
    matching a page where the panel was never confirmed. ``panel_expand``
    controls whether an expand control is present (count()==1) or absent.
    """

    def __init__(
        self,
        indexes,
        *,
        grow_indexes_on_reload=(),
        panel_titles: dict[str, bool] | None = None,
        panel_expand: bool = False,
    ):
        self.url = "https://hh.ru/resume/resume-1"
        self.indexes = list(indexes)
        self._grow_indexes_on_reload = list(grow_indexes_on_reload)
        self._reloaded = False
        self._panel_titles = panel_titles
        self._panel_expand = panel_expand
        self._panel_checkboxes: dict[str, _PanelCheckbox] = (
            {
                title: _PanelCheckbox(count=1, checked=checked)
                for title, checked in panel_titles.items()
            }
            if panel_titles is not None
            else {}
        )
        self.expand_clicked = False

    def locator(self, selector):
        if selector.startswith("[data-qa^='edit-experience-button-']"):
            return _RowButtonsLocator(self.indexes)
        if selector.startswith("xpath=") and "Развернуть" in selector:
            if self._panel_titles is None:
                return _Locator(count=0)
            if not self._panel_expand:
                return _Locator(count=0)
            page = self

            class _ExpandLocator(_Locator):
                def click(self, *, timeout=None, **_kwargs):
                    page.expand_clicked = True

            return _ExpandLocator(count=1)
        if selector.startswith("xpath=") and "этим местом работы" in selector:
            if self._panel_titles is None:
                return _Locator(count=0)
            return _PanelScopeLocator(self._panel_checkboxes)
        if "Развернуть" in selector:
            return _Locator(count=0)
        if "edit-experience-button" in selector:
            index = int(selector.rsplit("-", 1)[-1].rstrip("]").strip("'"))
            return _Locator(count=1 if index in self.indexes else 0)
        return _Locator(count=1)

    def wait_for_url(self, url, *, wait_until=None, timeout=None):
        return None

    def wait_for_function(self, _fn, *, arg=None, timeout=None):
        return None

    def wait_for_timeout(self, _ms):
        return None

    def reload(self, *, timeout=None, wait_until=None):
        self._reloaded = True
        self.indexes.extend(self._grow_indexes_on_reload)


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

    page = _SavePage(indexes=[], grow_indexes_on_reload=[2])
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

    page = _SavePage(indexes=[])
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
    set staying flat after reload must not be flagged as a binding failure;
    that check only applies to a genuinely new first_entry row. Index is
    deliberately non-zero (#815: a resume with one existing row does not
    necessarily expose it at index 0)."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _SavePage(indexes=[3], panel_titles={"Target Resume": True})
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[3],
        resume_titles={"resume-1": "Target Resume"},
    )

    assert results == [ExperienceResult("строка 3: сохранено и привязано к резюме", True)]


def test_edit_experience_existing_row_edit_requires_target_title(monkeypatch):
    """#782: an existing-row save also lands on the shared panel screen and
    must be reconciled — a caller that omits resume_titles is refused before
    any save click, not just the new-row (via_add_button) case."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)

    page = _SavePage(indexes=[3], panel_titles={"Target Resume": True})
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[3],
    )

    assert len(results) == 1
    assert not results[0].success
    assert "название целевого резюме" in results[0].reason


class _DisabledEndYearSavePage(_SavePage):
    """#800: end-year is disabled by default (checkbox "Работаю сейчас"
    checked) on a fresh first-entry form — the fixture that reproduces the
    original timeout bug (fill() retried against a disabled field)."""

    def __init__(self, *args, uncheck_enables=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._uncheck_enables = uncheck_enables
        self.checkbox_clicked = False

    def locator(self, selector):
        if selector == "[data-qa='resume-editor-experience-end-year-input']":
            enabled = self._uncheck_enables and self.checkbox_clicked
            return _Locator(count=1, enabled=enabled)
        if selector == "[data-qa='checkbox-container'] input":
            page = self

            class _CheckboxLocator(_Locator):
                def click(self, *, timeout=None, **_kwargs):
                    page.checkbox_clicked = True

            return _CheckboxLocator(count=1)
        return super().locator(selector)


def test_edit_experience_current_true_skips_disabled_end_year(monkeypatch):
    """#800: entry.current=True on a form whose end-year is already disabled
    (default state of a fresh entry) must not call fill() on it — that used
    to retry until Playwright's 30s timeout."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _DisabledEndYearSavePage(indexes=[], grow_indexes_on_reload=[2])
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer", current=True)]),
        dry_run=False,
    )

    assert results == [ExperienceResult("строка 0: сохранено и привязано к резюме", True)]
    assert page.checkbox_clicked is False


def test_edit_experience_current_false_unchecks_checkbox_to_unlock_end_year(monkeypatch):
    """#800: entry.current=False but the end-year field is still disabled
    (checkbox defaults to checked on a new entry) — the checkbox must be
    unchecked before filling end_year, not left blocking the field."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _DisabledEndYearSavePage(indexes=[], grow_indexes_on_reload=[2], uncheck_enables=True)
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan(
            [ExperienceEntry(company="Acme", position="Engineer", current=False, end_year="2024")]
        ),
        dry_run=False,
    )

    assert page.checkbox_clicked is True
    assert results == [ExperienceResult("строка 0: сохранено и привязано к резюме", True)]


class _UnreadableRowLocator(_Locator):
    def input_value(self):
        raise ValueError("поле определяется неоднозначно (0)")


class _ReadPage:
    """Two experience rows at non-contiguous indexes 1 and 2 (#815: hh.ru's
    row index is an internal counter, not a 0-based position). Row 1 is
    confirmed unreadable (drifted field), row 2 reads normally — #796's
    resilience fix must skip row 1, not fail the whole read."""

    def locator(self, selector):
        if selector.startswith("[data-qa^='edit-experience-button-']"):
            return _RowButtonsLocator([1, 2])
        if "Развернуть" in selector:
            return _Locator(count=0)
        if "edit-experience-button" in selector:
            index = int(selector.rsplit("-", 1)[-1].rstrip("]").strip("'"))
            return _Locator(count=1 if index in (1, 2) else 0)
        if "specific-company-input-1" in selector:
            return _UnreadableRowLocator(count=1)
        return _Locator(count=1)


def test_read_experience_skips_unreadable_row_instead_of_failing_whole_read(monkeypatch):
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)

    result = read_experience_on_hh(_ReadPage(), "resume-1")

    assert len(result) == 1


class _MultiRowReadPage:
    """#844 live trace: EXPERIENCE_EDIT_BUTTON's click navigates to a
    separate page (/profile/edit/experience/{rowId}), and EXPERIENCE_CANCEL
    was confirmed live to have no effect there — the row list on
    /resume/{resume_id} is only available again after a fresh navigation.
    This fake never "closes" the form on cancel(); it only resets once
    open_confirmed_resume() is called again, mirroring that finding.

    #844 PR review: a fresh open_confirmed_resume() navigation was ALSO
    confirmed live to re-collapse the row list to the first 3 buttons on
    any resume with more than 3 rows (same as _expand_experience_list()'s
    own documented reload-collapse behavior) — this fake models that too:
    every open_confirmed_resume() call resets `_expanded` to False, and
    only the first 3 indexes are visible until the "Развернуть" control is
    clicked again.
    """

    COLLAPSE_THRESHOLD = 3

    def __init__(self, indexes):
        self._indexes = list(indexes)
        self.open_confirmed_calls = 0
        self._form_open_for: int | None = None
        self._expanded = False

    def _visible_indexes(self):
        if self._expanded or len(self._indexes) <= self.COLLAPSE_THRESHOLD:
            return self._indexes
        return self._indexes[: self.COLLAPSE_THRESHOLD]

    def open_confirmed_resume_hook(self):
        """Called by the fake_open_confirmed_resume monkeypatch below."""
        self._form_open_for = None
        self._expanded = False

    def locator(self, selector):
        if selector.startswith("[data-qa^='edit-experience-button-']"):
            if self._form_open_for is not None:
                return _RowButtonsLocator([])
            return _RowButtonsLocator(self._visible_indexes())
        if "Развернуть" in selector:
            if self._form_open_for is not None or self._expanded:
                return _Locator(count=0)
            if len(self._indexes) > self.COLLAPSE_THRESHOLD:
                page = self

                class _ExpandLocator(_Locator):
                    def click(self, *, timeout=None, **_kwargs):
                        page._expanded = True

                return _ExpandLocator(count=1)
            return _Locator(count=0)
        if "edit-experience-button" in selector:
            index = int(selector.rsplit("-", 1)[-1].rstrip("]").strip("'"))
            if self._form_open_for is not None:
                return _Locator(count=0)
            if index in self._visible_indexes():
                page = self

                class _EditButtonLocator(_Locator):
                    def click(self, *, timeout=None, **_kwargs):
                        page._form_open_for = index

                return _EditButtonLocator(count=1)
            return _Locator(count=0)
        if f"specific-company-input-{self._form_open_for}" in selector:
            return _Locator(count=1)
        if "specific-company-input" in selector:
            # Company field for any OTHER index only exists once its own
            # edit button was clicked (#844: it does not exist in the DOM
            # ahead of that navigation).
            return _Locator(count=0)
        return _Locator(count=1)


def test_read_experience_reads_all_rows_when_cancel_click_has_no_effect(monkeypatch):
    """#844: EXPERIENCE_CANCEL's click was confirmed live to do nothing on
    the row-editor page — read_experience_on_hh must not rely on it to get
    back to the row list; every row must still be read, not just the first.

    6 rows on a resume (over the 3-row collapse threshold, matching the
    live-observed [1,6,7,8,12,17] from the issue) also exercises the #844
    PR-review finding: a fresh open_confirmed_resume() re-collapses the row
    list, so the fix must re-expand it on every iteration, not just once
    before the loop."""
    calls = {"count": 0}

    def fake_open_confirmed_resume(page, resume_id):
        calls["count"] += 1
        page.open_confirmed_resume_hook()

    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", fake_open_confirmed_resume)

    page = _MultiRowReadPage([1, 6, 7, 8, 12, 17])
    result = read_experience_on_hh(page, "resume-1")

    assert len(result) == 6
    # One open_confirmed_resume() call to enter + one per row to recover
    # from the row editor page, since EXPERIENCE_CANCEL cannot be relied on.
    assert calls["count"] == 1 + 6


def test_read_month_parses_selected_label_confirmed_live():
    """#811: the trigger is not an <input> — inner_text() is "Месяц" (unset)
    or "Месяц\\nМарт" (selected), confirmed live 2026-08-30."""
    from hhru_bot.experience import _read_month

    assert _read_month(_Locator(count=1, text="Месяц")) == ""
    assert _read_month(_Locator(count=1, text="Месяц\nМарт")) == "3"
    assert _read_month(_Locator(count=1, text="Месяц\nДекабрь")) == "12"


class _MonthOptionLocator(_Locator):
    """Simulates the month listbox option addressed by EXPERIENCE_MONTH_OPTION."""

    def __init__(self, page, month, *, count=1):
        super().__init__(count=count)
        self._page = page
        self._month = month

    def click(self, *, timeout=None, **_kwargs):
        self._page.selected_month = self._month
        self._page.listbox_open = False


class _MonthComboboxPage:
    """Minimal page fake for _select_month: tracks whether the listbox popup
    is open and which month option was clicked (confirmed live shape: click
    opens role='listbox' with 12 role='option' items keyed by
    magritte-select-option-{01..12})."""

    def __init__(self, *, option_count=1):
        self.listbox_open = False
        self.selected_month = None
        self._option_count = option_count

    def locator(self, selector):
        if "magritte-select-option-" in selector:
            month = selector.rsplit("-", 1)[-1].rstrip("]").strip("'")
            return _MonthOptionLocator(self, month, count=self._option_count)
        if selector == "[role='listbox']":
            page = self

            class _ListboxLocator(_Locator):
                def wait_for(self, *, timeout=None, state=None):
                    assert state == "hidden"
                    assert page.listbox_open is False

            return _ListboxLocator(count=0)
        raise AssertionError(f"unexpected selector: {selector}")


def test_select_month_clicks_confirmed_option_by_two_digit_number():
    from hhru_bot.experience import EXPERIENCE_MONTH_OPTION, _select_month

    page = _MonthComboboxPage()
    trigger = _Locator(count=1)
    _select_month(page, trigger, "3")

    assert page.selected_month == "03"
    assert EXPERIENCE_MONTH_OPTION.format(month="03") == "[data-qa='magritte-select-option-03']"


def test_select_month_rejects_invalid_month_number():
    from hhru_bot.experience import _select_month

    page = _MonthComboboxPage()
    trigger = _Locator(count=1)
    with pytest.raises(ValueError, match="1-12"):
        _select_month(page, trigger, "13")
    with pytest.raises(ValueError, match="1-12"):
        _select_month(page, trigger, "not-a-number")


def test_select_month_fails_closed_on_ambiguous_option():
    """Fail-closed (project invariant): more than one match after visibility
    is an anomaly, not "option not found" — must not click blindly."""
    from hhru_bot.experience import _select_month

    page = _MonthComboboxPage(option_count=2)
    trigger = _Locator(count=1)
    with pytest.raises(ValueError, match="неоднозначно"):
        _select_month(page, trigger, "3")


class _MonthSavePage(_SavePage):
    """#811: adds working start/end-month comboboxes on top of _SavePage's
    row-count tracking, so a save-path test can assert the month was
    actually selected before save is clicked."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_month_selected = None

    def locator(self, selector):
        if selector == "[data-qa='resume-editor-experience-start-month-input']":
            page = self

            class _StartMonthLocator(_Locator):
                def click(self, *, timeout=None, **_kwargs):
                    pass

                def inner_text(self):
                    # #956: _select_month_stable re-reads the trigger through
                    # _read_month; reflect the selection like the real
                    # combobox does ("Месяц\n<Название>").
                    if page.start_month_selected:
                        number = int(page.start_month_selected)
                        return f"Месяц\n{MONTH_NAMES[number - 1]}"
                    return "Месяц"

            return _StartMonthLocator(count=1)
        if "magritte-select-option-" in selector:
            page = self
            month = selector.rsplit("-", 1)[-1].rstrip("]").strip("'")

            class _OptionLocator(_Locator):
                def click(self, *, timeout=None, **_kwargs):
                    page.start_month_selected = month

            return _OptionLocator(count=1)
        if selector == "[role='listbox']":
            return _Locator(count=0)
        return super().locator(selector)


def test_edit_experience_selects_start_month_when_provided(monkeypatch):
    """#811 end-to-end: a plan entry with start_month set must drive a real
    click through the confirmed EXPERIENCE_MONTH_OPTION selector before
    save, not just carry the value in the dataclass."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _MonthSavePage(indexes=[], grow_indexes_on_reload=[2])
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan(
            [
                ExperienceEntry(
                    company="Acme", position="Engineer", start_year="2020", start_month="3"
                )
            ]
        ),
        dry_run=False,
    )

    assert page.start_month_selected == "03"
    assert results == [ExperienceResult("строка 0: сохранено и привязано к резюме", True)]


def test_cli_entry_requires_start_month():
    """#811: hh.ru's form will not save without a start month — --entry must
    fail closed at parse time with a clear reason, not silently drop the
    field and let the save land in `uncertain` further downstream."""
    from hhru_bot.commands.edit_experience import _load_entries

    with pytest.raises(ValueError, match="start_month"):
        _load_entries(
            ['{"company":"Acme","position":"Engineer","start_year":"2020"}'],
        )


def test_cli_entry_accepts_explicit_start_month():
    from hhru_bot.commands.edit_experience import _load_entries

    entries = _load_entries(
        ['{"company":"Acme","position":"Engineer","start_year":"2020","start_month":"3"}'],
    )
    assert entries[0].start_month == "3"


class _NonContiguousRowsPage:
    """#815 live finding: hh.ru's row indexes are a non-contiguous internal
    counter (confirmed live: 2,3,4 collapsed / 2,3,4,8,9 expanded — never
    starting at 0). A resume with 3 rows can equally well expose them at
    indexes 2/3/4 as at 0/1/2; range(0, count) silently returns 0 whenever
    index 0 happens to be unused, which is the common case."""

    def __init__(self, indexes, *, has_expand=False):
        self.url = "https://hh.ru/resume/resume-1"
        self._indexes = list(indexes)
        self._has_expand = has_expand
        self.expand_clicked = False

    def locator(self, selector):
        if selector.startswith("[data-qa^='edit-experience-button-']"):
            return _RowButtonsLocator(self._indexes)
        if "Развернуть" in selector:
            if self._has_expand and not self.expand_clicked:
                return _ExpandLocator(self)
            return _Locator(count=0)
        if "edit-experience-button" in selector:
            index = int(selector.rsplit("-", 1)[-1].rstrip("]").strip("'"))
            return _Locator(count=1 if index in self._indexes else 0)
        return _Locator(count=1)


class _ExpandLocator(_Locator):
    """#815: clicking "Развернуть" reveals the rows hidden behind hh.ru's
    collapse threshold — modeled here as swapping in the full index set and
    hiding the control itself, matching the live-confirmed behavior."""

    def __init__(self, page):
        super().__init__(count=1)
        self._page = page

    def click(self, *, timeout=None, **_kwargs):
        self._page.expand_clicked = True
        self._page._indexes = [2, 3, 4, 8, 9]


def test_experience_row_indexes_are_not_contiguous_from_zero():
    """#815: range(0, N) undercounts (or returns 0) whenever a non-contiguous
    index set does not start at 0 — the actual bug behind the false [FAIL]/
    uncertain on a resume with real, saved experience rows."""
    page = _NonContiguousRowsPage([2, 3, 4])

    assert _experience_row_indexes(page) == [2, 3, 4]


def test_experience_row_indexes_expands_collapsed_list():
    """#815: hh.ru collapses the experience list to 3 visible cards behind a
    "Развернуть" control once a resume has more than 3 entries — the real
    row count is only visible after that control is clicked."""
    page = _NonContiguousRowsPage([2, 3, 4], has_expand=True)

    assert _experience_row_indexes(page) == [2, 3, 4, 8, 9]
    assert page.expand_clicked is True


def test_edit_experience_fails_closed_on_nonexistent_index_without_titles(monkeypatch):
    """#782/#787/#840: a resume that already has rows and is asked to CREATE
    a new one (requested index not currently in use) now goes through the
    shared-add shape (via_add_button) instead of the old unconditional
    #815 refusal — but it must still fail closed, before any click, when the
    caller has not supplied resume_titles for the binding panel."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    page = _NonContiguousRowsPage([2, 3, 4])

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
    )

    assert len(results) == 1
    assert not results[0].success
    assert not results[0].uncertain
    assert "не найден среди существующих строк" in results[0].reason
    assert "не передано" in results[0].reason


class _AddButtonPage:
    """#782: fake for the shared-add shape (EXPERIENCE_ADD_BUTTON on a
    resume that already has rows). Starts with ``initial_indexes`` rows (all
    considered "not the requested one", forcing via_add_button); the panel
    starts fully checked, mirroring the live-confirmed default for a NEW
    row (#782 comment 5) — reconciliation must uncheck every non-target
    title. ``panel_titles`` is required for a working test; the "expand"
    control is present whenever there are more than 2 titles, matching the
    live-confirmed collapse threshold.
    """

    def __init__(
        self,
        initial_indexes,
        panel_titles: dict[str, bool],
        *,
        has_expand=None,
        expand_swallowed_clicks: int = 0,
    ):
        self.url = "https://hh.ru/resume/resume-1"
        self.indexes = list(initial_indexes)
        self._reloaded = False
        self._panel_checkboxes = {
            title: _PanelCheckbox(count=1, checked=checked)
            for title, checked in panel_titles.items()
        }
        self._has_expand = has_expand if has_expand is not None else len(panel_titles) > 2
        # #858: the first N clicks on the expand control are "swallowed" by
        # the pre-hydration window — click() succeeds but the button stays
        # visible (wait_for(state="hidden") raises), mirroring the live drill.
        self._expand_swallowed_clicks = expand_swallowed_clicks
        self.expand_attempts = 0
        self.expand_clicked = False
        self.add_clicked = False

    def locator(self, selector):
        if selector.startswith("[data-qa^='edit-experience-button-']"):
            return _RowButtonsLocator(self.indexes)
        if "Развернуть" in selector and "resume-list-card-experience" in selector:
            # The experience-card row-list expand control (EXPERIENCE_EXPAND_BUTTON) —
            # distinct from the panel's own expand, never present in these fixtures.
            return _Locator(count=0)
        if "edit-experience-button" in selector:
            index = int(selector.rsplit("-", 1)[-1].rstrip("]").strip("'"))
            return _Locator(count=1 if index in self.indexes else 0)
        if selector == "[data-qa='resume-list-card-experience'] [data-qa='link']":
            page = self

            class _AddTriggerLocator(_Locator):
                def click(self, *, timeout=None, **_kwargs):
                    page.add_clicked = True

            return _AddTriggerLocator(count=1)
        if selector.startswith("xpath=") and "Развернуть" in selector:
            if not self._has_expand:
                return _Locator(count=0)
            page = self

            class _ExpandLocator(_Locator):
                def click(self, *, timeout=None, **_kwargs):
                    page.expand_attempts += 1
                    if page.expand_attempts > page._expand_swallowed_clicks:
                        page.expand_clicked = True

                def wait_for(self, *, timeout=None, state=None):
                    if state == "hidden" and page.expand_attempts <= page._expand_swallowed_clicks:
                        raise PlaywrightError("swallowed by pre-hydration window (#858)")
                    return None

            return _ExpandLocator(count=1)
        if selector.startswith("xpath=") and "этим местом работы" in selector:
            return _PanelScopeLocator(
                self._panel_checkboxes,
                visible_count=2,
                is_expanded=lambda: self.expand_clicked,
            )
        return _Locator(count=1)

    def wait_for_url(self, url, *, wait_until=None, timeout=None):
        return None

    def wait_for_function(self, _fn, *, arg=None, timeout=None):
        # #858: hydration readiness probe — the fake's expand control is
        # treated as hydrated immediately.
        return None

    def wait_for_timeout(self, _ms):
        return None

    def reload(self, *, timeout=None, wait_until=None):
        self._reloaded = True
        self.indexes.append(99)  # simulate hh.ru creating the new row


def _add_button_fixture(monkeypatch, page):
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)


def test_edit_experience_via_add_button_refuses_unconfirmed_other_title(monkeypatch):
    """Codex review (PR #958 round 2): a resume whose panel title could not
    be confirmed (empty string from list_resume_cards) is dropped from
    other_titles by the `and title` filter, so reconciliation would never
    uncheck it — on a NEW row its pre-checked box would silently over-bind
    the entry. The save must be refused up front instead."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage(
        [2, 3],
        panel_titles={"Target Resume": True},
    )

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
        resume_titles={"resume-1": "Target Resume", "other-1": ""},
    )

    assert page.add_clicked is False
    assert len(results) == 1
    assert not results[0].success
    assert "не подтверждено" in results[0].reason
    assert "other-1" in results[0].reason


def test_edit_experience_via_add_button_unchecks_other_resumes_before_save(monkeypatch):
    """#782: the core silent-over-binding fix — a new row on a non-empty
    resume must have every OTHER account resume's checkbox unchecked before
    save, leaving only the target resume checked."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage(
        [2, 3],
        panel_titles={
            "Target Resume": True,
            "ai-engineer": True,
            "ai-teamlead": True,
            "python": True,
        },
    )

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
        resume_titles={
            "resume-1": "Target Resume",
            "other-1": "ai-engineer",
            "other-2": "ai-teamlead",
            "other-3": "python",
        },
    )

    assert page.add_clicked is True
    assert page._panel_checkboxes["Target Resume"].is_checked() is True
    assert page._panel_checkboxes["ai-engineer"].is_checked() is False
    assert page._panel_checkboxes["ai-teamlead"].is_checked() is False
    assert page._panel_checkboxes["python"].is_checked() is False
    assert results == [ExperienceResult("строка 7: сохранено и привязано к резюме", True)]


def test_edit_experience_via_add_button_handles_apostrophe_in_resume_title(monkeypatch):
    """PR review (#856): a resume title is untrusted free text and can
    contain an apostrophe (a plausible Russian title) — reconciliation must
    resolve it via accessible-name matching (get_by_role), not a hand-built
    CSS attribute selector, or one such title would break checkbox lookup
    for every OTHER resume in the account too, not just the one with the
    quote. This test would fail with the old
    input[aria-label='{title}']-based fake, whose naive split/rstrip
    parsing breaks on an embedded quote the same way the real CSS selector
    would."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage(
        [2, 3],
        panel_titles={
            "Target Resume": True,
            "Data Engineer's Profile": True,
        },
    )

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
        resume_titles={"resume-1": "Target Resume", "other": "Data Engineer's Profile"},
    )

    assert page._panel_checkboxes["Target Resume"].is_checked() is True
    assert page._panel_checkboxes["Data Engineer's Profile"].is_checked() is False
    assert results == [ExperienceResult("строка 7: сохранено и привязано к резюме", True)]


def test_reconcile_experience_resume_panel_existing_row_only_touches_target(monkeypatch):
    """#856 review (non-blocking observation): a direct unit test for
    is_new_row=False — editing an EXISTING row must leave every OTHER
    resume's checkbox exactly as found (their state is that row's real
    binding, not a default to discard) and only ensure the target one ends
    up checked."""
    from hhru_bot.experience import _reconcile_experience_resume_panel

    checkboxes = {
        "Target Resume": _PanelCheckbox(count=1, checked=False),
        "ai-engineer": _PanelCheckbox(count=1, checked=True),
        "python": _PanelCheckbox(count=1, checked=False),
    }
    page = _AddButtonPage([2, 3], panel_titles={})
    page._panel_checkboxes = checkboxes

    _reconcile_experience_resume_panel(
        page,
        target_title="Target Resume",
        other_titles=["ai-engineer", "python"],
        is_new_row=False,
    )

    assert checkboxes["Target Resume"].is_checked() is True
    assert checkboxes["ai-engineer"].is_checked() is True
    assert checkboxes["python"].is_checked() is False


def test_edit_experience_via_add_button_expands_collapsed_panel(monkeypatch):
    """#782: with more than 2 account resumes the panel starts collapsed —
    reconciliation must click "Развернуть" before touching any checkbox, or
    the non-visible ones could never be found/unchecked."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage(
        [2, 3],
        panel_titles={"Target Resume": True, "ai-engineer": True, "python": True},
    )

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
        resume_titles={"resume-1": "Target Resume", "o1": "ai-engineer", "o2": "python"},
    )

    assert page.expand_clicked is True
    assert results == [ExperienceResult("строка 7: сохранено и привязано к резюме", True)]


def test_edit_experience_via_add_button_retries_swallowed_expand_clicks(monkeypatch):
    """#858: hh.ru server-renders the expand button before React hydrates it
    (live drill 2026-08-30: clicks at 1.6-2.3s after load were silently
    swallowed, the first click after __reactFiber/__reactProps attached
    worked). Reconciliation must retry the click — with post-click
    confirmation — instead of failing on the first swallowed one."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage(
        [2, 3],
        panel_titles={"Target Resume": True, "ai-engineer": True, "python": True},
        expand_swallowed_clicks=2,
    )

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
        resume_titles={"resume-1": "Target Resume", "o1": "ai-engineer", "o2": "python"},
    )

    assert page.expand_attempts == 3  # two swallowed, third confirmed
    assert results == [ExperienceResult("строка 7: сохранено и привязано к резюме", True)]


def test_edit_experience_via_add_button_fails_closed_when_all_expand_clicks_swallowed(monkeypatch):
    """#858 fail-closed: if every retry is swallowed (list never expands),
    reconciliation must still refuse before any save click — the retry loop
    narrows the failure window, it does not lower the confirmation bar."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage(
        [2, 3],
        panel_titles={"Target Resume": True, "ai-engineer": True, "python": True},
        expand_swallowed_clicks=99,
    )

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
        resume_titles={"resume-1": "Target Resume", "o1": "ai-engineer", "o2": "python"},
    )

    assert page.expand_attempts == 5  # EXPAND_RETRY_ATTEMPTS, then give up
    assert len(results) == 1
    assert not results[0].success
    assert "не удалось развернуть панель" in results[0].reason


def test_edit_experience_append_only_forces_add_shape_on_index_collision(monkeypatch):
    """#957: append_only forces the shared add shape even when the requested
    index COLLIDES with an existing row (trigger.count()==1). Without it the
    plan would land on that row's editor and overwrite it (the #815 trap the
    manual path used to refuse outright); the colliding index must not
    address anything under append_only."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage([0, 3], panel_titles={"Target Resume": True, "python": True})

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[0],
        resume_titles={"resume-1": "Target Resume", "other": "python"},
        append_only=True,
    )

    assert page.add_clicked is True
    assert results == [ExperienceResult("строка 0: сохранено и привязано к резюме", True)]


def test_edit_experience_via_add_button_fails_closed_when_row_set_does_not_grow(monkeypatch):
    """#957: the add shape CREATES a row, so the post-save row set must grow
    exactly like first_entry — a flat set after reload means the row was not
    created or not bound to THIS resume, and must be a definite failure, not
    a silent success."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _SavePage(indexes=[3], panel_titles={"Target Resume": True})
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[5],
        resume_titles={"resume-1": "Target Resume"},
    )

    assert page._reloaded is True
    assert len(results) == 1
    assert not results[0].success
    assert not results[0].uncertain
    assert "не привязалась к резюме" in results[0].reason


def test_edit_experience_via_add_button_fails_closed_when_checkbox_count_mismatches(monkeypatch):
    """#782 fail-closed guard: if the panel does not expose exactly one
    checkbox per expected account resume title (e.g. the list never
    actually expanded), reconciliation must refuse before any save click —
    a partial reconciliation is worse than none."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage([2, 3], panel_titles={"Target Resume": True})

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
        resume_titles={"resume-1": "Target Resume", "o1": "missing-from-panel"},
    )

    assert len(results) == 1
    assert not results[0].success
    assert "не найден однозначно" in results[0].reason


def test_edit_experience_via_add_button_fails_closed_when_expand_missing_before_save(monkeypatch):
    """#782: a panel that should have collapsed extra rows (>2 account
    resumes) but exposes no expand control at all is an unconfirmed/drifted
    state — reconciliation must refuse rather than silently accept whatever
    subset of checkboxes happens to be present."""
    _add_button_fixture(monkeypatch, None)
    page = _AddButtonPage(
        [2, 3],
        panel_titles={"Target Resume": True, "ai-engineer": True, "python": True},
        has_expand=False,
    )
    # Without expand, only 2 of the 3 titles are "visible" in this fixture —
    # simulate the drifted state by leaving one checkbox entirely absent.
    del page._panel_checkboxes["python"]

    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
        indexes=[7],
        resume_titles={"resume-1": "Target Resume", "o1": "ai-engineer", "o2": "python"},
    )

    assert len(results) == 1
    assert not results[0].success
    assert "не найден однозначно" in results[0].reason


def test_shared_experience_save_cancel_use_distinct_profile_layout_namespace():
    """#840: the third (shared-profile-editor) shape's save/cancel controls
    live in a THIRD data-qa namespace (profile-layout-*-button), distinct
    from both the indexed row editor (resume-*) and the first-entry editor
    (resume-partial-edit-*) — a regression here would silently point a
    future integration at the wrong shape's buttons."""
    assert SHARED_EXPERIENCE_SAVE == "[data-qa='profile-layout-save-button']"
    assert SHARED_EXPERIENCE_CANCEL == "[data-qa='profile-layout-cancel-button']"
    save_and_cancel_from_other_shapes = {
        EXPERIENCE_COMPANY,
        EXPERIENCE_POSITION,
        FIRST_EXPERIENCE_CURRENT_CHECKBOX,
    }
    assert SHARED_EXPERIENCE_SAVE not in save_and_cancel_from_other_shapes
    assert SHARED_EXPERIENCE_CANCEL not in save_and_cancel_from_other_shapes


def test_shared_experience_reuses_indexed_company_position_and_month_selectors():
    """#840 live finding: the third shape's company/position fields use the
    exact same indexed data-qa pattern as the indexed row editor
    (resume-profile-experience-specific-{company,position}-input-{index}),
    and its month combobox popup was JS-confirmed to render the same
    magritte-select-option-{NN} / role=listbox structure as the already
    working EXPERIENCE_START_MONTH/END_MONTH — so no new constants were
    needed for those fields, only for save/cancel."""
    assert (
        EXPERIENCE_COMPANY == "[data-qa='resume-profile-experience-specific-company-input-{index}']"
    )
    assert (
        EXPERIENCE_POSITION
        == "[data-qa='resume-profile-experience-specific-position-input-{index}']"
    )
    assert EXPERIENCE_MONTH_OPTION == "[data-qa='magritte-select-option-{month}']"
    assert EXPERIENCE_MONTH_LISTBOX == "[role='listbox']"


class _ValidationErrorsLocator(_Locator):
    """Fake for the form-helper-error group locator (#958 follow-up):
    count()==len(texts), nth(i) serves each message via inner_text()."""

    def __init__(self, texts):
        super().__init__(count=len(texts))
        self._texts = list(texts)

    def nth(self, i):
        return _Locator(count=1, text=self._texts[i])


class _RejectedSavePage(_SavePage):
    """#958 follow-up (live capture 2026-09-03): the save click does not
    navigate — wait_for_url times out Playwright-style while hh.ru keeps the
    editor URL and renders inline validation errors under the empty required
    fields. ``error_texts=None`` models a timeout with NO readable messages
    (the true-unknown outcome that must stay uncertain)."""

    def __init__(self, indexes, error_texts, **kwargs):
        super().__init__(indexes, **kwargs)
        self._error_texts = error_texts

    def wait_for_url(self, url, *, wait_until=None, timeout=None):
        raise PlaywrightError(
            f"Timeout {timeout}ms exceeded.\nwaiting for navigation to {url} until '{wait_until}'"
        )

    def locator(self, selector):
        if selector == "validation-errors":
            if self._error_texts is None:
                return _Locator(count=0)
            return _ValidationErrorsLocator(self._error_texts)
        return super().locator(selector)


class _WipeOnceLocator(_Locator):
    """#956 wipe-window model: the value survives every pre-save read but is
    gone once the save click lands; a refill holds it."""

    def __init__(self):
        super().__init__(count=1)
        self._wiped = False

    def input_value(self):
        if self._wiped:
            return ""
        return getattr(self, "_filled_value", "")

    def fill(self, value):
        self._wiped = False
        super().fill(value)

    def wipe(self):
        self._wiped = True


class _WipeAlwaysLocator(_WipeOnceLocator):
    """A field that holds until the save click wipes it and that even a
    refill cannot hold afterwards — the fail-closed refill check must stop
    the retry before a blind second save."""

    def input_value(self):
        if self._wiped:
            return ""
        return getattr(self, "_filled_value", "")

    def fill(self, value):
        # record like the base class, but a wiped field never recovers
        self._filled_value = value


class _RejectionRetryPage(_SavePage):
    """Save click #1 is rejected by client-side validation (no navigation,
    form-helper-error visible), exactly like the live #956 wipe-window run.
    ``reject_clicks`` = how many consecutive save clicks hh.ru rejects;
    the first rejection arms a one-off wipe of the description field, the
    refill the retry performs must restore it."""

    def __init__(self, indexes, reject_clicks=1, wipe_holds=True, **kwargs):
        super().__init__(indexes, **kwargs)
        self._reject_clicks = reject_clicks
        self.save_clicks = 0
        self._description = _WipeOnceLocator() if wipe_holds else _WipeAlwaysLocator()

    def wait_for_url(self, url, *, wait_until=None, timeout=None):
        if self.save_clicks <= self._reject_clicks:
            raise PlaywrightError(
                f"Timeout {timeout}ms exceeded.\nwaiting for navigation to {url} until '{wait_until}'"
            )

    def locator(self, selector):
        if selector == "validation-errors":
            if self.save_clicks <= self._reject_clicks:
                return _ValidationErrorsLocator(["Пожалуйста, укажите"])
            return _Locator(count=0)
        if selector == "[data-qa='resume-editor-experience-description-input']":
            return self._description
        if selector == "[data-qa='resume-partial-edit-save']":

            class _SaveButton(_Locator):
                def __init__(self, page):
                    super().__init__(count=1)
                    self._page = page

                def click(self, *, timeout=None, **_kwargs):
                    self._page.save_clicks += 1
                    if self._page.save_clicks == 1:
                        # the #956 async wipe lands between the last
                        # verification and the submit
                        self._page._description.wipe()

            return _SaveButton(self)
        return super().locator(selector)


def test_edit_experience_save_rejection_retry_refills_wiped_field_and_saves(monkeypatch):
    """Live 2026-09-03 (dump-confirmed): hh.ru rejected the save over ONE
    empty field although every pre-save check had just passed — the #956
    wipe landed between the last verification and the submit. The rejection
    must be answered with ONE bounded refill+verify pass and a second save
    click, which here navigates: success, no uncertain, no silent loss."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr(
        "hhru_bot.experience.EXPERIENCE_SAVE_VALIDATION_ERRORS", "validation-errors"
    )
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)
    dumps = []
    monkeypatch.setattr(
        "hhru_bot.experience._dump_experience_save_failure",
        lambda page, index, exc: dumps.append(index),
    )

    page = _RejectionRetryPage(indexes=[], reject_clicks=1, grow_indexes_on_reload=[2])
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan(
            [ExperienceEntry(company="Acme", position="Engineer", duties="Готовил хлеб")]
        ),
        dry_run=False,
    )

    assert page.save_clicks == 2
    assert page._description.input_value() == "Готовил хлеб"
    assert results == [ExperienceResult("строка 0: сохранено и привязано к резюме", True)]
    assert dumps == [0]


class _DetachingRefillLocator(_Locator):
    """A control the remount removes entirely, but only AFTER the first save
    click: input_value() raises the same PlaywrightError the real "not
    attached" state produces, so the pre-save pass (which must keep working)
    sees a normal filled field and the post-rejection refill hits the crash."""

    def __init__(self, page):
        super().__init__(count=1)
        self._page = page

    def input_value(self):
        if self._page.save_clicks >= 1:
            raise PlaywrightError("Element is not attached to the DOM")
        return getattr(self, "_filled_value", "")

    def wipe(self):
        # the base _RejectionRetryPage save-click hook calls this; a detached
        # node needs no state change — reads already raise past this point
        return None


def test_edit_experience_refill_crash_after_rejection_reports_uncertain(monkeypatch):
    """The refill pass runs INSIDE the save-timeout except handler, so a
    PlaywrightError/ValueError raised there must not escape uncaught and
    crash the command (Python handlers do not catch exceptions raised in
    sibling handlers) — a save click already happened, so the row outcome
    is uncertain, not a traceback."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr(
        "hhru_bot.experience.EXPERIENCE_SAVE_VALIDATION_ERRORS", "validation-errors"
    )
    dumps = []
    monkeypatch.setattr(
        "hhru_bot.experience._dump_experience_save_failure",
        lambda page, index, exc: dumps.append(index),
    )

    page = _RejectionRetryPage(indexes=[], reject_clicks=1)
    page._description = _DetachingRefillLocator(page)
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan(
            [ExperienceEntry(company="Acme", position="Engineer", duties="Готовил хлеб")]
        ),
        dry_run=False,
    )

    assert page.save_clicks == 1
    assert len(results) == 1
    assert not results[0].success
    assert results[0].uncertain
    assert "дозаполнение после отклонения" in results[0].reason
    assert dumps == [0]


def test_edit_experience_save_rejected_after_retry_reports_definite_failure(monkeypatch):
    """A rejection that SURVIVES the refill pass is final: plain failed with
    the validation text (retryable, no uncertain lock), never a blind third
    save click."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr(
        "hhru_bot.experience.EXPERIENCE_SAVE_VALIDATION_ERRORS", "validation-errors"
    )
    dumps = []
    monkeypatch.setattr(
        "hhru_bot.experience._dump_experience_save_failure",
        lambda page, index, exc: dumps.append(index),
    )

    page = _RejectionRetryPage(indexes=[], reject_clicks=2)
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan(
            [ExperienceEntry(company="Acme", position="Engineer", duties="Готовил хлеб")]
        ),
        dry_run=False,
    )

    assert page.save_clicks == 2
    assert len(results) == 1
    assert not results[0].success
    assert not results[0].uncertain
    assert "после дозаполнения" in results[0].reason
    assert "Пожалуйста, укажите" in results[0].reason
    assert dumps == [0, 0]


def test_edit_experience_refill_failure_after_rejection_never_re_saves(monkeypatch):
    """If the refilled value cannot hold (settle check fails), the retry
    stops BEFORE the second save click — a guaranteed-empty form must not be
    resubmitted."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr(
        "hhru_bot.experience.EXPERIENCE_SAVE_VALIDATION_ERRORS", "validation-errors"
    )
    dumps = []
    monkeypatch.setattr(
        "hhru_bot.experience._dump_experience_save_failure",
        lambda page, index, exc: dumps.append(index),
    )

    page = _RejectionRetryPage(indexes=[], reject_clicks=1, wipe_holds=False)
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan(
            [ExperienceEntry(company="Acme", position="Engineer", duties="Готовил хлеб")]
        ),
        dry_run=False,
    )

    assert page.save_clicks == 1
    assert len(results) == 1
    assert not results[0].success
    assert not results[0].uncertain
    assert "не удерживает значение" in results[0].reason
    assert dumps == [0]


def test_edit_experience_save_timeout_without_validation_stays_uncertain(monkeypatch):
    """The rejection read is fail-safe: when the navigation wait times out
    and NO visible validation messages can be read, the outcome is genuinely
    unknown — the pre-existing uncertain + dump path is preserved."""
    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr(
        "hhru_bot.experience.EXPERIENCE_SAVE_VALIDATION_ERRORS", "validation-errors"
    )
    dumps = []
    monkeypatch.setattr(
        "hhru_bot.experience._dump_experience_save_failure",
        lambda page, index, exc: dumps.append(index),
    )

    page = _RejectedSavePage(indexes=[], error_texts=None)
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
    )

    assert len(results) == 1
    assert not results[0].success
    assert results[0].uncertain
    assert "сохранение не подтверждено после клика" in results[0].reason
    assert dumps == [0]


class _QuerySuffixNavPage(_SavePage):
    """wait_for_url with real full-match glob semantics (fnmatch): the
    post-save redirect lands on /resume/{id}?hhtmFrom=profile_experience,
    the exact live-observed shape (#958 follow-up)."""

    def wait_for_url(self, pattern, *, wait_until=None, timeout=None):
        import fnmatch

        target = "https://hh.ru/resume/resume-1?hhtmFrom=profile_experience"
        if not fnmatch.fnmatch(target, pattern):
            raise PlaywrightError(
                f"Timeout {timeout}ms exceeded.\n"
                f"waiting for navigation to \"{pattern}\" until '{wait_until}'"
            )
        self.url = target


def test_edit_experience_save_redirect_with_query_suffix_is_success(monkeypatch):
    """#958 follow-up (live log 2026-09-03): hh.ru redirects a SUCCESSFUL
    save to ".../resume/{id}?hhtmFrom=profile_experience"; a bare
    "**/resume/{id}" glob is a full match, so wait_for_url timed out and the
    save was recorded uncertain although Playwright's own log showed the
    navigation ("navigated to ..."). The wait pattern must match the query
    suffix; identity/binding re-verification below still guards the result."""
    import fnmatch  # noqa: F401 - mirrors the fake's matching semantics

    monkeypatch.setattr("hhru_bot.experience.open_confirmed_resume", lambda page, resume_id: None)
    monkeypatch.setattr("hhru_bot.experience.goto_hh", _fake_goto_to_edit_path)
    monkeypatch.setattr("hhru_bot.experience.resume_identity_matches", lambda page, resume_id: True)
    monkeypatch.setattr("hhru_bot.experience.require_authenticated_page", lambda page: None)

    page = _QuerySuffixNavPage(indexes=[], grow_indexes_on_reload=[2])
    results = edit_experience_on_hh(
        page,
        "resume-1",
        ExperiencePlan([ExperienceEntry(company="Acme", position="Engineer")]),
        dry_run=False,
    )

    assert results == [ExperienceResult("строка 0: сохранено и привязано к резюме", True)]

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hhru_bot.resume_position as resume_position
from hhru_bot.config import bare_resume
from hhru_bot.resume_position import (
    TITLE,
    PositionValues,
    build_position_prompt,
    fill_only_missing,
    parse_position_response,
    validate_wizard_plan,
)
from hhru_bot.resume_state import ResumeProfessionalRole, ResumeState

pytestmark = pytest.mark.unit


def test_position_response_accepts_structured_values_without_inventing_salary():
    plan = parse_position_response(
        '{"title":"Backend engineer","salary":null,"currency":null,'
        '"specializations":[],"employment":["full_time"],'
        '"work_format":["remote"],"commute":"no_limit","business_trips":false}'
    )
    assert plan == PositionValues(
        title="Backend engineer",
        salary=None,
        currency=None,
        specializations=[],
        employment=["full_time"],
        work_format=["remote"],
        commute="no_limit",
        business_trips=False,
    )


def test_position_response_rejects_unknown_enum():
    with pytest.raises(ValueError, match="employment enum"):
        parse_position_response('{"employment":["whatever"]}')


def test_position_response_rejects_non_string_title():
    with pytest.raises(ValueError, match="title"):
        parse_position_response('{"title":{"role":"Backend"}}')


def test_prompt_contains_mode_and_current_values():
    messages = build_position_prompt(
        type("Profile", (), {"desired_role": "Python developer", "skills": ["Python"]})(),
        PositionValues(title="Existing", salary=None),
        "fill",
    )
    assert messages[0]["role"] == "system"
    assert "salary" in messages[0]["content"]
    assert '"mode": "fill"' in messages[1]["content"]
    assert '"Existing"' in messages[1]["content"]


def test_fill_mode_preserves_existing_values():
    current = PositionValues(title="Existing", employment=["full_time"], business_trips=False)
    plan = PositionValues(
        title="New",
        salary=100000,
        employment=["remote"],
        commute="up_to_1_hour",
        business_trips=True,
    )
    merged = fill_only_missing(current, plan)
    assert merged.title is None
    assert merged.employment is None
    assert merged.business_trips is None
    assert merged.salary == 100000


def test_wizard_plan_requires_exactly_one_catalog_role():
    with pytest.raises(RuntimeError, match="ровно одна профессия"):
        validate_wizard_plan(PositionValues(title="Повар"))
    with pytest.raises(RuntimeError, match="ровно одна профессия"):
        validate_wizard_plan(PositionValues(title="Повар", specializations=["Повар", "Шеф-повар"]))


def test_wizard_plan_rejects_fields_from_later_steps():
    with pytest.raises(RuntimeError, match="salary"):
        validate_wizard_plan(
            PositionValues(title="Повар", specializations=["Повар"], salary=100000)
        )


def test_position_wizard_is_bound_to_resume_query():
    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/professional_role?resume=resume-id"

    assert resume_position.is_position_wizard(page, "resume-id") is True
    assert resume_position.is_position_wizard(page, "other-id") is False


def test_open_position_form_reads_draft_wizard_title(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/professional_role?resume=resume-id"
    position = MagicMock()
    position.count.return_value = 1
    position.first = position
    position.evaluate.return_value = True
    position.input_value.return_value = "AI Team Lead"
    page.locator.side_effect = lambda selector: position
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)

    flow = resume_position.open_position_form(page, resume)

    assert flow.kind == "wizard"
    assert flow.resume_id == "resume-id"
    assert flow.values.title == "AI Team Lead"


def test_open_position_form_routes_profile_state_to_identity_bound_wizard(monkeypatch):
    """Regression: hh.ru may keep /resume/<id> despite professional_role state."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    page.content.return_value = (
        '{"scheme":{"nextIncompleteScreenId":"professional_role"},'
        '"resume":{"id":"resume-id","status":"not_finished",'
        '"isSearchable":false,"canPublishOrUpdate":false}}'
    )
    position = MagicMock()
    position.count.return_value = 1
    position.first = position
    position.evaluate.return_value = True
    position.input_value.return_value = "AI Engineer"
    page.locator.return_value = position
    visited = []

    def goto(_page, url):
        visited.append(url)
        page.url = url

    monkeypatch.setattr(resume_position, "goto_hh", goto)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)

    flow = resume_position.open_position_form(page, resume)

    assert flow.kind == "wizard"
    assert flow.state.next_incomplete_screen_id == "professional_role"
    assert flow.values.title == "AI Engineer"
    assert visited == [
        "https://hh.ru/resume/resume-id",
        "https://hh.ru/profile/resume/professional_role?resume=resume-id",
    ]


def test_open_position_form_rejects_wrong_wizard_resume(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/professional_role?resume=other-id"
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)

    with pytest.raises(RuntimeError, match="не для того резюме"):
        resume_position.open_position_form(page, resume)


def test_open_position_form_rejects_ambiguous_wizard_entry_card(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/professional_role?resume=resume-id"
    position = MagicMock()
    position.count.return_value = 0
    select_job = MagicMock()
    select_job.count.return_value = 2
    select_job.first = select_job
    page.locator.side_effect = lambda selector: {
        resume_position.WIZARD_POSITION: position,
        resume_position.WIZARD_SELECT_JOB: select_job,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)

    with pytest.raises(RuntimeError, match="карточка выбора профессии неоднозначна: 2"):
        resume_position.open_position_form(page, resume)


def test_open_position_form_retries_ssr_card_until_hydrated(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/professional_role?resume=resume-id"
    position = MagicMock()
    position.count.side_effect = [0, 0, 1, 1, 1]
    position.first = position
    position.evaluate.return_value = True
    position.input_value.return_value = "AI Engineer"
    select_job = MagicMock()
    select_job.count.return_value = 1
    select_job.first = select_job
    page.locator.side_effect = lambda selector: {
        resume_position.WIZARD_POSITION: position,
        resume_position.WIZARD_SELECT_JOB: select_job,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)

    flow = resume_position.open_position_form(page, resume)

    assert flow.kind == "wizard"
    assert flow.values.title == "AI Engineer"
    select_job.click.assert_called_once_with(timeout=resume_position.WIZARD_TRANSITION_POLL_MS)
    page.wait_for_timeout.assert_called_once_with(resume_position.WIZARD_TRANSITION_POLL_MS)
    page.reload.assert_not_called()


def test_open_position_form_reloads_once_after_stalled_ssr_card(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/professional_role?resume=resume-id"
    position = MagicMock()
    position.count.side_effect = [0, 0, 0, 1, 1, 1]
    position.first = position
    position.evaluate.return_value = True
    position.input_value.return_value = "AI Engineer"
    select_job = MagicMock()
    select_job.count.return_value = 1
    select_job.first = select_job
    page.locator.side_effect = lambda selector: {
        resume_position.WIZARD_POSITION: position,
        resume_position.WIZARD_SELECT_JOB: select_job,
    }[selector]
    monkeypatch.setattr(resume_position, "WIZARD_TRANSITION_ATTEMPTS", 2)
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)

    flow = resume_position.open_position_form(page, resume)

    assert flow.kind == "wizard"
    page.reload.assert_called_once_with(wait_until="domcontentloaded")
    assert select_job.click.call_count == 2


def test_save_position_wizard_clears_inherited_role_and_selects_exact_catalog_role():
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/professional_role?resume=resume-id"
    position = MagicMock()
    position.count.return_value = 1
    position.input_value.return_value = ""
    clear = MagicMock()
    clear.count.return_value = 1
    next_button = MagicMock()
    next_button.count.return_value = 1
    next_button.first = next_button
    search = MagicMock()
    search.count.return_value = 1
    search.first = search
    checkbox = MagicMock()
    # hh.ru repeats the same semantic role under several tree categories.
    checkbox.count.return_value = 2
    checkbox.first = checkbox
    checkbox.nth.return_value = checkbox
    row = MagicMock()
    row.count.return_value = 1
    text = MagicMock()
    text.count.return_value = 1
    text.first = text
    text.inner_text.return_value = "Аналитик"
    checkbox.locator.return_value = row
    row.locator.return_value = text
    submit = MagicMock()
    submit.count.return_value = 1
    submit.click.side_effect = lambda: setattr(
        page, "url", "https://hh.ru/applicant/resumes/suitable_vacancies?published=true"
    )
    page.locator.side_effect = lambda selector: {
        resume_position.WIZARD_POSITION: position,
        resume_position.WIZARD_POSITION_CLEAR: clear,
        resume_position.WIZARD_NEXT: next_button,
        resume_position.WIZARD_CATEGORY_SEARCH: search,
        resume_position.WIZARD_CATEGORY_INPUT.format("10"): checkbox,
        resume_position.WIZARD_CATEGORY_SUBMIT: submit,
    }[selector]
    before_first_click = MagicMock()

    resume_position.save_position_wizard(
        page,
        resume,
        PositionValues(title="AI Engineer", specializations=["Аналитик"]),
        role_id="10",
        before_first_click=before_first_click,
    )

    clear.click.assert_called_once_with()
    position.fill.assert_called_once_with("AI Engineer")
    before_first_click.assert_called_once_with()
    next_button.click.assert_called_once_with()
    search.fill.assert_called_once_with("Аналитик")
    checkbox.check.assert_called_once_with()
    submit.click.assert_called_once_with()


def test_save_position_wizard_clicks_final_next_when_catalog_only_closes_modal():
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/profile/resume/professional_role?resume=resume-id"
    position = MagicMock()
    position.count.return_value = 1
    position.input_value.return_value = ""
    clear = MagicMock()
    clear.count.return_value = 1
    next_button = MagicMock()
    next_button.count.return_value = 1
    next_button.first = next_button
    search = MagicMock()
    search.count.return_value = 1
    search.first = search
    checkbox = MagicMock()
    checkbox.count.return_value = 1
    checkbox.first = checkbox
    checkbox.nth.return_value = checkbox
    row = MagicMock()
    row.count.return_value = 1
    text = MagicMock()
    text.count.return_value = 1
    text.first = text
    text.inner_text.return_value = "Аналитик"
    checkbox.locator.return_value = row
    row.locator.return_value = text
    submit = MagicMock()
    submit.count.return_value = 1
    next_button.click.side_effect = lambda: setattr(
        page, "url", "https://hh.ru/applicant/resumes/suitable_vacancies?published=true"
    )
    page.locator.side_effect = lambda selector: {
        resume_position.WIZARD_POSITION: position,
        resume_position.WIZARD_POSITION_CLEAR: clear,
        resume_position.WIZARD_NEXT: next_button,
        resume_position.WIZARD_CATEGORY_SEARCH: search,
        resume_position.WIZARD_CATEGORY_INPUT.format("10"): checkbox,
        resume_position.WIZARD_CATEGORY_SUBMIT: submit,
    }[selector]

    resume_position.save_position_wizard(
        page,
        resume,
        PositionValues(title="AI Engineer", specializations=["Аналитик"]),
        role_id="10",
    )

    next_button.click.assert_called_once_with()


def test_apply_position_rejects_empty_title_without_touching_dom():
    page = MagicMock()

    with pytest.raises(ValueError, match="Пустой title отклоняется hh.ru"):
        resume_position.apply_position(page, PositionValues(title=""))

    page.locator.assert_not_called()


def test_apply_position_none_title_leaves_unchanged():
    page = MagicMock()
    title = MagicMock()
    page.locator.side_effect = lambda selector: title if selector == TITLE else MagicMock()

    resume_position.apply_position(page, PositionValues(title=None))

    title.fill.assert_not_called()


def test_apply_position_rejects_multiple_employment_values():
    page = MagicMock()

    with pytest.raises(RuntimeError, match="несколько значений --employment не подтверждены"):
        resume_position.apply_position(page, PositionValues(employment=["full_time", "part_time"]))

    page.locator.assert_not_called()


def test_apply_position_rejects_multiple_work_format_values():
    page = MagicMock()

    with pytest.raises(RuntimeError, match="несколько значений --work-format не подтверждены"):
        resume_position.apply_position(page, PositionValues(work_format=["remote", "hybrid"]))

    page.locator.assert_not_called()


def test_apply_position_clicks_currency_chip_label_not_radio_input():
    # #785: the data-qa element is a visible role="radio" input whose click
    # target is intercepted by its chip <span>; the real hit target is the
    # wrapping <label> chip, resolved from the confirmed radio via xpath.
    page = MagicMock()
    currency_input = MagicMock()
    currency_input.count.return_value = 1
    currency_radio = MagicMock()
    currency_radio.count.return_value = 1
    currency_chip = MagicMock()
    currency_chip.count.return_value = 1
    currency_radio.locator.return_value = currency_chip
    page.locator.return_value = currency_input
    page.get_by_role.return_value = currency_radio

    resume_position.apply_position(page, PositionValues(currency="RUR"))

    currency_input.click.assert_not_called()
    currency_radio.click.assert_not_called()
    page.get_by_role.assert_called_once_with("radio", name="Рубли", exact=True)
    currency_radio.locator.assert_called_once_with("xpath=ancestor::label[1]")
    currency_chip.click.assert_called_once_with()


def test_set_control_reopens_dropdown_for_each_value():
    class FakeOption:
        def __init__(self, panel, label):
            self.panel = panel
            self.label = label

        def count(self):
            return 1

        def click(self):
            self.panel.selected.append(self.label)

    class FakePanel:
        def __init__(self):
            self.open = False
            self.selected = []

        def wait_for(self, *, state, timeout=None):
            assert self.open is (state == "visible")

        def get_by_role(self, role, *, name, exact):
            assert (role, exact) == ("option", True)
            return FakeOption(self, name)

    class FakeControl:
        def __init__(self, panel):
            self.panel = panel
            self.clicks = 0
            self.first = self

        def count(self):
            return 1

        def evaluate(self, _script):
            return "BUTTON"

        def click(self):
            self.clicks += 1
            self.panel.open = not self.panel.open

    panel = FakePanel()
    control = FakeControl(panel)
    page = MagicMock()
    page.locator.side_effect = lambda selector: (
        panel if selector == resume_position.RESUME_POSITION_DROPDOWN else control
    )

    resume_position._set_control(
        page, resume_position.WORK_FORMAT, "remote", resume_position.WORK_LABELS
    )
    resume_position._set_control(
        page, resume_position.WORK_FORMAT, "hybrid", resume_position.WORK_LABELS
    )

    assert panel.selected == ["Удалённо", "Гибрид"]
    assert control.clicks == 4


def test_set_control_passes_explicit_timeout_to_panel_waits():
    """#561 review: an unlabeled 30s default hang read as CLI silence."""
    panel = MagicMock()
    panel.wait_for.return_value = None
    option = MagicMock()
    option.count.return_value = 1
    panel.get_by_role.return_value = option
    control = MagicMock()
    control.count.return_value = 1
    control.first = control
    control.evaluate.return_value = "BUTTON"
    page = MagicMock()
    page.locator.side_effect = lambda selector: (
        panel if selector == resume_position.RESUME_POSITION_DROPDOWN else control
    )

    resume_position._set_control(
        page, resume_position.WORK_FORMAT, "remote", resume_position.WORK_LABELS
    )

    for call in panel.wait_for.call_args_list:
        assert call.kwargs["timeout"] == resume_position._CONTROL_WAIT_TIMEOUT_MS


def test_set_control_dumps_dom_on_timeout(monkeypatch):
    """#561 review: a live single-value run failed with no captured evidence."""
    from playwright.sync_api import Error as PlaywrightError

    panel = MagicMock()
    panel.wait_for.side_effect = PlaywrightError("Timeout 5000ms exceeded.")
    control = MagicMock()
    control.count.return_value = 1
    control.first = control
    control.evaluate.return_value = "BUTTON"
    page = MagicMock()
    page.locator.side_effect = lambda selector: (
        panel if selector == resume_position.RESUME_POSITION_DROPDOWN else control
    )
    dump = MagicMock()
    monkeypatch.setattr(resume_position, "_dump_control_failure", dump)

    with pytest.raises(PlaywrightError):
        resume_position._set_control(
            page, resume_position.WORK_FORMAT, "remote", resume_position.WORK_LABELS
        )

    dump.assert_called_once()
    assert dump.call_args.args[0] is page
    assert dump.call_args.args[1] == resume_position.WORK_FORMAT


def test_apply_position_sets_confirmed_specializations(monkeypatch):
    page = MagicMock()
    set_specializations = MagicMock()
    monkeypatch.setattr(resume_position, "_set_specializations", set_specializations)

    resume_position.apply_position(page, PositionValues(specializations=["Аналитик"]))

    set_specializations.assert_called_once_with(page, ["Аналитик"])


def test_open_position_form_retries_pre_hydration_noop_click(monkeypatch):
    """#337: an SSR anchor has no handler until hydration, and URL stays put."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/resume-id"
    edit = MagicMock()
    edit.count.return_value = 1
    form = MagicMock()
    form.count.return_value = 0
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    form.wait_for.side_effect = [
        PlaywrightTimeoutError("not hydrated"),
        None,
    ]

    def click_side_effect():
        # The second attempt's click is the one that actually hydrates and
        # commits the dedicated edit route.
        if edit.click.call_count == 2:
            page.url = "https://hh.ru/resume/edit/resume-id/position"

    edit.click.side_effect = click_side_effect
    page.locator.side_effect = lambda selector: {
        resume_position.EDIT: edit,
        resume_position.FORM: form,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(
        resume_position,
        "parse_resume_state",
        lambda *_args: ResumeState(status="new", is_searchable=True),
    )
    monkeypatch.setattr(resume_position, "read_display_position", lambda _page: PositionValues())
    monkeypatch.setattr(resume_position, "read_position", lambda _page: PositionValues())

    resume_position.open_position_form(page, resume)

    assert edit.click.call_count == 2
    assert form.wait_for.call_count == 2
    form.wait_for.assert_called_with(state="visible", timeout=30_000)
    page.wait_for_url.assert_not_called()


def test_open_position_form_rejects_form_on_wrong_resume_route(monkeypatch):
    """#337 follow-up: a visible form must belong to the requested resume_id.

    The pre-#337 code enforced this via ``wait_for_url`` before querying the
    form. Dropping that wait must not drop the invariant it protected: a
    visible ``FORM`` on an unexpected edit route (e.g. hh.ru routed the click
    to a different resume) must still fail closed instead of being read as
    the requested resume's position.
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    # The form is visible, but the committed route belongs to a different resume.
    page.url = "https://hh.ru/resume/edit/other-resume-id/position"
    edit = MagicMock()
    edit.count.return_value = 1
    form = MagicMock()
    form.count.return_value = 0
    form.wait_for.return_value = None
    page.locator.side_effect = lambda selector: {
        resume_position.EDIT: edit,
        resume_position.FORM: form,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(
        resume_position,
        "parse_resume_state",
        lambda *_args: ResumeState(status="new", is_searchable=True),
    )
    monkeypatch.setattr(resume_position, "read_display_position", lambda _page: PositionValues())
    read_position = MagicMock(return_value=PositionValues())
    monkeypatch.setattr(resume_position, "read_position", read_position)

    with pytest.raises(
        RuntimeError, match="форма редактирования позиции открыта не для того резюме"
    ):
        resume_position.open_position_form(page, resume)
    read_position.assert_not_called()


def test_open_position_form_accepts_correct_edit_route_on_first_attempt(monkeypatch):
    """Positive counterpart: the post-condition must accept the real edit route.

    Pins the check against `edit_path` specifically — a check comparing
    against `profile_path` (or any other constant) instead would either
    reject this legitimate first-attempt success or accept the wrong-route
    cases the tests above cover, so this test and those must both pass only
    together with the correct comparison.
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/resume-id/position"
    edit = MagicMock()
    edit.count.return_value = 1
    form = MagicMock()
    form.count.return_value = 0
    form.wait_for.return_value = None
    page.locator.side_effect = lambda selector: {
        resume_position.EDIT: edit,
        resume_position.FORM: form,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(
        resume_position,
        "parse_resume_state",
        lambda *_args: ResumeState(status="new", is_searchable=True),
    )
    monkeypatch.setattr(resume_position, "read_display_position", lambda _page: PositionValues())
    monkeypatch.setattr(resume_position, "read_position", lambda _page: PositionValues())

    flow = resume_position.open_position_form(page, resume)

    assert flow.kind == "editor"
    assert edit.click.call_count == 1
    assert form.wait_for.call_count == 1


def test_open_position_form_rejects_already_mounted_form_on_wrong_route(monkeypatch):
    """The route post-condition must not be bypassable via a pre-mounted form.

    When ``FORM.count() != 0`` the whole click/retry block is skipped
    entirely. If the page happens to already show a position form for a
    different resume (stale editor state, unexpected redirect), that form
    must still be rejected, not read as the requested resume's position.
    """
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/other-resume-id/position"
    form = MagicMock()
    form.count.return_value = 1
    page.locator.side_effect = lambda selector: {
        resume_position.FORM: form,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(
        resume_position,
        "parse_resume_state",
        lambda *_args: ResumeState(status="new", is_searchable=True),
    )
    monkeypatch.setattr(resume_position, "read_display_position", lambda _page: PositionValues())
    read_position = MagicMock(return_value=PositionValues())
    monkeypatch.setattr(resume_position, "read_position", read_position)

    with pytest.raises(
        RuntimeError, match="форма редактирования позиции открыта не для того резюме"
    ):
        resume_position.open_position_form(page, resume)
    read_position.assert_not_called()


def test_verify_wizard_save_polls_until_professional_role_clears(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    states = iter(
        [
            ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
            ResumeState(
                status="new",
                is_searchable=True,
                professional_roles=(ResumeProfessionalRole("10", "Аналитик"),),
            ),
        ]
    )
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(resume_position, "parse_resume_state", lambda *_args: next(states))
    monkeypatch.setattr(resume_position.time, "monotonic", MagicMock(side_effect=[0, 0, 1]))
    monkeypatch.setattr(
        "hhru_bot.copy_resume.list_resume_cards",
        lambda *_args, **_kwargs: [SimpleNamespace(resume_id="resume-id", title="AI Engineer")],
    )

    state = resume_position.verify_wizard_save(
        page,
        resume,
        expected_title="AI Engineer",
        expected_role_id="10",
        expected_role_label="Аналитик",
    )

    assert state.is_searchable is True
    page.wait_for_timeout.assert_called_once_with(resume_position.WIZARD_VERIFY_POLL_MS)


def test_verify_wizard_save_polls_until_resume_card_title_matches(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    state = ResumeState(
        status="new",
        is_searchable=True,
        professional_roles=(ResumeProfessionalRole("10", "Аналитик"),),
    )
    cards = iter(
        [
            [SimpleNamespace(resume_id="resume-id", title="Old title")],
            [SimpleNamespace(resume_id="resume-id", title="AI Engineer")],
        ]
    )
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(resume_position, "parse_resume_state", lambda *_args: state)
    monkeypatch.setattr(resume_position.time, "monotonic", MagicMock(side_effect=[0, 0, 1]))
    monkeypatch.setattr(
        "hhru_bot.copy_resume.list_resume_cards",
        lambda *_args, **_kwargs: next(cards),
    )

    result = resume_position.verify_wizard_save(
        page,
        resume,
        expected_title="AI Engineer",
        expected_role_id="10",
        expected_role_label="Аналитик",
    )

    assert result is state
    page.wait_for_timeout.assert_called_once_with(resume_position.WIZARD_VERIFY_POLL_MS)


def test_open_position_form_accepts_edit_route_with_query(monkeypatch):
    """Query parameters on the edit route must not break the route guard."""
    resume = bare_resume("resume-id")
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/resume-id/position?foo=bar"
    edit = MagicMock()
    edit.count.return_value = 1
    form = MagicMock()
    form.count.return_value = 0
    form.wait_for.return_value = None
    page.locator.side_effect = lambda selector: {
        resume_position.EDIT: edit,
        resume_position.FORM: form,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(
        resume_position,
        "parse_resume_state",
        lambda *_args: ResumeState(status="new", is_searchable=True),
    )
    monkeypatch.setattr(resume_position, "read_display_position", lambda _page: PositionValues())
    monkeypatch.setattr(resume_position, "read_position", lambda _page: PositionValues())

    flow = resume_position.open_position_form(page, resume)

    assert flow.kind == "editor"
    assert edit.click.call_count == 1
    assert form.wait_for.call_count == 1


def test_open_position_form_rejects_wrong_route_with_empty_resume_id(monkeypatch):
    """An empty resume_id must not accidentally match a different edit route."""
    resume = bare_resume("")
    page = MagicMock()
    page.url = "https://hh.ru/resume/edit/other-resume-id/position"
    edit = MagicMock()
    edit.count.return_value = 1
    form = MagicMock()
    form.count.return_value = 0
    form.wait_for.return_value = None
    page.locator.side_effect = lambda selector: {
        resume_position.EDIT: edit,
        resume_position.FORM: form,
    }[selector]
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(
        resume_position,
        "parse_resume_state",
        lambda *_args: ResumeState(status="new", is_searchable=True),
    )
    monkeypatch.setattr(resume_position, "read_display_position", lambda _page: PositionValues())
    read_position = MagicMock(return_value=PositionValues())
    monkeypatch.setattr(resume_position, "read_position", read_position)

    with pytest.raises(
        RuntimeError, match="форма редактирования позиции открыта не для того резюме"
    ):
        resume_position.open_position_form(page, resume)
    read_position.assert_not_called()


def test_verify_wizard_save_never_accepts_persistent_professional_role(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    state = ResumeState(status="not_finished", next_incomplete_screen_id="professional_role")
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(resume_position, "parse_resume_state", lambda *_args: state)
    monkeypatch.setattr(resume_position.time, "monotonic", MagicMock(side_effect=[0, 0, 31]))

    with pytest.raises(RuntimeError, match="всё ещё показывает professional_role"):
        resume_position.verify_wizard_save(
            page,
            resume,
            expected_title="AI Engineer",
            expected_role_id="10",
            expected_role_label="Аналитик",
        )


def test_verify_wizard_save_rejects_wrong_server_role(monkeypatch):
    resume = bare_resume("resume-id")
    page = MagicMock()
    state = ResumeState(
        status="new",
        is_searchable=True,
        professional_roles=(ResumeProfessionalRole("96", "Программист, разработчик"),),
    )
    monkeypatch.setattr(resume_position, "goto_hh", lambda *_args: None)
    monkeypatch.setattr(resume_position, "require_authenticated_page", lambda _page: None)
    monkeypatch.setattr(resume_position, "resume_identity_matches", lambda *_args: True)
    monkeypatch.setattr(resume_position, "parse_resume_state", lambda *_args: state)
    monkeypatch.setattr(resume_position.time, "monotonic", MagicMock(side_effect=[0, 0, 31]))
    monkeypatch.setattr(
        "hhru_bot.copy_resume.list_resume_cards",
        lambda *_args, **_kwargs: [SimpleNamespace(resume_id="resume-id", title="AI Engineer")],
    )

    with pytest.raises(RuntimeError, match="ожидалось 10:Аналитик"):
        resume_position.verify_wizard_save(
            page,
            resume,
            expected_title="AI Engineer",
            expected_role_id="10",
            expected_role_label="Аналитик",
        )

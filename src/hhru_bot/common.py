"""UI editing of the simple fields on a resume's ``common`` screen (#876).

This module intentionally contains no transport code: navigation and saving are
performed only by the visible hh.ru form.  The selectors below are the
data-qa handles confirmed during the read-only common-screen probe.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    HH_BASE_URL,
    goto_hh,
    labelled_field,
    open_hydrated_resume_editor,
    require_authenticated_page,
)
from .config import ResumeConfig
from .selector_groups.resume_page import RESUME_POSITION_DROPDOWN

FORM = "[data-qa='resume-edit-common-form']"
EDIT = "[data-qa='resume-edit-common-button']"
FIRST_NAME = "[data-qa='resume-edit-firstName']"
LAST_NAME = "[data-qa='resume-edit-lastName']"
BIRTHDAY = "[data-qa='resume-edit-birthday']"
GENDER = "[data-qa='resume-edit-gender']"
PHONE = "[data-qa='resume-edit-phone']"
AREA = "[data-qa='resume-edit-area']"
METRO = "[data-qa='resume-edit-metro']"
CITIZENSHIP = "[data-qa='resume-edit-citizenship']"
TREE_MODAL = "[data-qa='tree-selector-modal']"
TREE_SEARCH = "[data-qa='tree-selector-search-input']"
TREE_OPTION = "[data-qa^='tree-selector-item tree-selector-item-'][data-qa*='tree-selector-child-']"
TREE_SUBMIT = "[data-qa='tree-selector-submit']"
WORK_TICKET = "Наличие трудовой книжки"
RELOCATION = "Готовность к переезду"
SCHEDULE = "График работы"
EMPLOYMENT = "Тип занятости"
WORK_FORMAT = "Формат работы"
BUSINESS_TRIP = "Готовность к командировкам"
SCHEDULE_LABELS = {
    "full_day": "Полный день",
    "shift": "Сменный график",
    "flexible": "Гибкий график",
    "remote": "Удалённая работа",
}
EMPLOYMENT_LABELS = {
    "full_time": "Постоянная работа",
    "part_time": "Подработка",
    "internship": "Стажировка",
    "volunteer": "Волонтёрство",
}
WORK_FORMAT_LABELS = {"office": "Офис", "hybrid": "Гибрид", "remote": "Удалённо"}
SAVE = "[data-qa='resume-partial-edit-save']"
CANCEL = "[data-qa='resume-partial-edit-cancel']"
_WAIT_MS = 5_000


@dataclass(frozen=True)
class CommonValues:
    first_name: str | None = None
    last_name: str | None = None
    birthday: str | None = None
    gender: str | None = None
    phone: str | None = None
    area: str | None = None
    metro: list[str] | None = None
    citizenship: list[str] | None = None
    work_ticket: str | None = None
    relocation: str | None = None
    schedule: list[str] | None = None
    employment: list[str] | None = None
    work_format: list[str] | None = None
    business_trip: str | None = None

    def provided(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "firstName": self.first_name,
                "lastName": self.last_name,
                "birthday": self.birthday,
                "gender": self.gender,
                "phone": self.phone,
                "area": self.area,
                "metro": self.metro,
                "citizenship": self.citizenship,
                "workTicket": self.work_ticket,
                "relocation": self.relocation,
                "schedule": self.schedule,
                "employment": self.employment,
                "work_format": self.work_format,
                "businessTrip": self.business_trip,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class CommonResult:
    success: bool
    reason: str
    acted: bool = False
    uncertain: bool = False


def _strict(page: Page, selector: str, label: str):
    loc = page.locator(selector)
    if loc.count() != 1:
        raise RuntimeError(f"поле {label} не подтверждено однозначно")
    return loc.first


def open_common_form(page: Page, resume: ResumeConfig):
    """Open and identity-bind the common editor; never guess from a redirect."""
    resume_id = resume.resume_id
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
    require_authenticated_page(page)
    editor = open_hydrated_resume_editor(
        page,
        trigger_selector=EDIT,
        editor_selector=FORM,
        profile_path=f"/resume/{resume_id}",
        edit_path=f"/resume/edit/{resume_id}/common",
        click_trigger=True,
        trigger_error="кнопка common не подтверждена",
        open_error="форма common не открылась",
        wrong_route_error="форма common открыта не для того резюме",
    )
    # The click above starts a React render.  Do not perform strict field
    # counts until the form is visibly mounted (commit != rendered).
    editor.first.wait_for(state="visible", timeout=_WAIT_MS)
    return editor


def read_common(page: Page) -> CommonValues:
    """Read only the fields owned by this slice from an already-open form."""

    def value(selector: str) -> str:
        loc = _strict(page, selector, selector)
        return loc.input_value().strip()

    def labelled_value(label: str):
        field = labelled_field(page, label)
        tag = field.evaluate("e=>e.tagName")
        if tag == "SELECT" and field.get_attribute("multiple") is not None:
            return field.evaluate("e=>Array.from(e.selectedOptions).map(o=>o.value)")
        if tag in ("INPUT", "TEXTAREA"):
            return field.input_value().strip()
        # Magritte may bind a label to a div/button trigger rather than an
        # input; its visible text is the readable state.
        return field.inner_text().strip()

    return CommonValues(
        first_name=value(FIRST_NAME),
        last_name=value(LAST_NAME),
        birthday=value(BIRTHDAY),
        gender=value(GENDER),
        phone=value(PHONE),
        area=value(AREA),
        metro=None,
        citizenship=None,
        work_ticket=labelled_value(WORK_TICKET),
        relocation=labelled_value(RELOCATION),
        schedule=labelled_value(SCHEDULE),
        employment=labelled_value(EMPLOYMENT),
        work_format=labelled_value(WORK_FORMAT),
        business_trip=labelled_value(BUSINESS_TRIP),
    )


def _set_tree(page: Page, trigger_selector: str, values: list[str], label: str) -> None:
    """Select exact leaves in a common-screen tree selector.

    A leaf can be rendered once per parent category.  Its complete data-qa is
    the identity, so repeated identical IDs are accepted while different IDs
    for one label are rejected.
    """
    trigger = _strict(page, trigger_selector, label)
    trigger.click()
    modal = page.locator(TREE_MODAL)
    search = modal.locator(TREE_SEARCH)
    submit = modal.locator(TREE_SUBMIT)
    if modal.count() != 1 or search.count() != 1 or submit.count() != 1:
        raise RuntimeError(f"панель выбора {label} не подтверждена")
    modal.first.wait_for(state="visible", timeout=_WAIT_MS)
    # The picker is multi-select and keeps its previous state between opens.
    # Clear each selected leaf by its stable id, not once per category row:
    # hh.ru may render the same leaf under several parent categories.
    # Keep the state probe independent from the option's compound data-qa
    # selector; hh.ru has used both a plain item class and the tree-item class
    # on selected rows across these pickers.
    selected = modal.locator("[aria-selected='true'][data-qa*='tree-selector-child-']")
    selected_ids = {
        selected.nth(index).get_attribute("data-qa") for index in range(selected.count())
    }
    for selected_id in selected_ids:
        if selected_id:
            modal.locator(f"[data-qa='{selected_id}']").first.click()

    for value in values:
        search.first.fill(value)
        option = modal.locator(TREE_OPTION).filter(has_text=re.compile(rf"^{re.escape(value)}$"))
        try:
            # fill starts an async React tree render; count() alone races it.
            option.first.wait_for(state="visible", timeout=_WAIT_MS)
        except PlaywrightError as exc:
            raise RuntimeError(f"{label} не найдено в дереве: {value}") from exc
        ids = {option.nth(index).get_attribute("data-qa") for index in range(option.count())}
        if not ids or len(ids) != 1:
            raise RuntimeError(f"вариант {label} не найден однозначно: {value}")
        option.first.click()
    submit.first.click()
    modal.first.wait_for(state="hidden", timeout=_WAIT_MS)


def apply_common(page: Page, values: CommonValues) -> None:
    """Fill explicit values only; all controls must resolve exactly once."""
    selectors = {
        "first_name": (FIRST_NAME, values.first_name),
        "last_name": (LAST_NAME, values.last_name),
        "birthday": (BIRTHDAY, values.birthday),
        "gender": (GENDER, values.gender),
        "phone": (PHONE, values.phone),
    }
    for name, (selector, value) in selectors.items():
        if value is not None:
            loc = _strict(page, selector, name)
            if name == "gender":
                loc.select_option(value)
            else:
                loc.fill(value)
    if values.area is not None:
        _set_tree(page, AREA, [values.area], "area")
    if values.metro is not None:
        _set_tree(page, METRO, values.metro, "metro")
    if values.citizenship is not None:
        _set_tree(page, CITIZENSHIP, values.citizenship, "citizenship")

    controls = (
        (WORK_TICKET, values.work_ticket, {"true": "Да", "false": "Нет"}),
        (
            RELOCATION,
            values.relocation,
            {
                "ready": "Готов к переезду",
                "consider": "Рассмотрю",
                "not_ready": "Не готов к переезду",
            },
        ),
        (BUSINESS_TRIP, values.business_trip, {"true": "Могу", "false": "Не могу"}),
    )
    for label, value, labels in controls:
        if value is not None:
            _set_control(page, labelled_field(page, label), value, labels)
    for label, value, labels in (
        (SCHEDULE, values.schedule, SCHEDULE_LABELS),
        (EMPLOYMENT, values.employment, EMPLOYMENT_LABELS),
        (WORK_FORMAT, values.work_format, WORK_FORMAT_LABELS),
    ):
        if value is not None:
            _set_many(page, labelled_field(page, label), value, labels)


def _set_control(page, field, value: str, labels: dict[str, str]) -> None:
    """Set a labelled native/custom single-choice control without guessing."""
    if value not in labels:
        raise ValueError(f"недопустимое значение common: {value}")
    tag = field.evaluate("e=>e.tagName")
    if tag == "SELECT":
        field.select_option(value)
    elif tag == "INPUT" and field.get_attribute("type") == "checkbox":
        (field.check if value == "true" else field.uncheck)()
    else:
        # Magritte renders these as a labelled trigger.  The caller's exact
        # label binding is the identity check; the option's exact accessible
        # name is the value check.
        field.click()
        popup = page.locator(RESUME_POSITION_DROPDOWN)
        popup.wait_for(state="visible", timeout=_WAIT_MS)
        options = popup.get_by_role("option", name=labels[value], exact=True)
        if options.count() != 1:
            raise RuntimeError(f"вариант формы не найден: {labels[value]}")
        options.first.click()
        page.mouse.click(0, 0)
        popup.wait_for(state="hidden", timeout=_WAIT_MS)


def _set_many(page, field, values: list[str], labels: dict[str, str]) -> None:
    if not isinstance(values, list):
        raise ValueError("мультивыбор common должен быть списком значений")
    unknown = [value for value in values if value not in labels]
    if unknown:
        raise ValueError(f"недопустимое значение common: {unknown[0]}")
    tag = field.evaluate("e=>e.tagName")
    if tag == "SELECT":
        field.select_option(values)
        return
    # A checkbox collection is returned by the exact labelled binding.  Set
    # the requested state explicitly, so repeated application is idempotent
    # and stale selections are removed rather than silently retained.
    field.click()
    popup = page.locator(RESUME_POSITION_DROPDOWN)
    popup.wait_for(state="visible", timeout=_WAIT_MS)
    options = popup.get_by_role("option")
    wanted = {labels[value] for value in values}
    for index in range(options.count()):
        option = options.nth(index)
        if (
            option.get_attribute("aria-selected") == "true"
            and option.inner_text().strip() not in wanted
        ):
            option.click()
    for value in values:
        option = popup.get_by_role("option", name=labels[value], exact=True)
        if option.count() != 1:
            raise RuntimeError(f"вариант формы не найден: {value}")
        if option.first.get_attribute("aria-selected") != "true":
            option.first.click()
    page.mouse.click(0, 0)
    popup.wait_for(state="hidden", timeout=_WAIT_MS)


def save_common(
    page: Page,
    values: CommonValues,
    *,
    before_click: Callable[[], None] | None = None,
) -> CommonResult:
    apply_common(page, values)
    save = _strict(page, SAVE, "кнопка сохранения common")
    if before_click:
        before_click()
    try:
        save.click()
        page.locator(FORM).first.wait_for(state="hidden", timeout=_WAIT_MS)
    except Exception as exc:  # click may have reached hh.ru: caller records uncertain
        return CommonResult(False, f"сохранение common не подтверждено: {exc}", True, True)
    return CommonResult(True, "поля common сохранены", True)

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

from .browser import HH_BASE_URL, goto_hh, open_hydrated_resume_editor, require_authenticated_page
from .config import ResumeConfig

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

    return CommonValues(
        first_name=value(FIRST_NAME),
        last_name=value(LAST_NAME),
        birthday=value(BIRTHDAY),
        gender=value(GENDER),
        phone=value(PHONE),
        area=value(AREA),
        metro=None,
        citizenship=None,
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
    selected = modal.locator(f"{TREE_OPTION}[aria-selected='true']")
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

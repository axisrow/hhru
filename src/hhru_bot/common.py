"""UI editing of the simple fields on a resume's ``common`` screen (#876).

This module intentionally contains no transport code: navigation and saving are
performed only by the visible hh.ru form.  The selectors below are the
data-qa handles confirmed during the read-only common-screen probe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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

    def provided(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "firstName": self.first_name,
                "lastName": self.last_name,
                "birthday": self.birthday,
                "gender": self.gender,
                "phone": self.phone,
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
    )


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

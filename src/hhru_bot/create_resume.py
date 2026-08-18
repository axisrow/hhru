"""Browser flow for creating an empty resume through the hh.ru UI (#304)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import HH_BASE_URL, goto_hh
from .selector_groups.resume_page import (
    RESUME_CREATE_BUTTON,
    RESUME_CREATION_AREA,
    RESUME_CREATION_SUBMIT,
    RESUME_CREATION_TITLE,
)

RESUMES_LIST_URL = f"{HH_BASE_URL}/applicant/resumes"
CREATION_URL = f"{HH_BASE_URL}/resume/creation"
_RESUME_ID_RE = re.compile(r"/resume/([0-9a-f]{32,40})(?:[/?#]|$)")


@dataclass
class CreateResumeResult:
    success: bool
    new_resume_id: str = ""
    reason: str = ""
    uncertain: bool = False


def _one(page: Page, selector: str, label: str):
    locator = page.locator(selector)
    count = locator.count()
    if count != 1:
        return None, f"{label} не подтверждён однозначно (совпадений: {count})"
    return locator.first, ""


def create_resume_on_hh(page: Page, *, area: str, title: str, dry_run: bool) -> CreateResumeResult:
    """Create one draft; never uses a direct HTTP request.

    Dry-run only reads the list and wizard DOM.  In particular it never clicks
    the list button, inputs, or submit control.
    """
    goto_hh(page, RESUMES_LIST_URL)
    create_button, reason = _one(page, RESUME_CREATE_BUTTON, "кнопка создания резюме")
    if reason:
        return CreateResumeResult(False, reason=reason)

    if dry_run:
        goto_hh(page, CREATION_URL)
    else:
        try:
            create_button.click()
            page.wait_for_url("**/resume/creation**", wait_until="commit")
        except PlaywrightError as exc:
            return CreateResumeResult(False, reason=f"не удалось открыть визард: {exc}")

    area_control, reason = _one(page, RESUME_CREATION_AREA, "поле профобласти")
    if reason:
        return CreateResumeResult(False, reason=reason)
    title_control, reason = _one(page, RESUME_CREATION_TITLE, "поле должности")
    if reason:
        return CreateResumeResult(False, reason=reason)
    submit, reason = _one(page, RESUME_CREATION_SUBMIT, "кнопка сохранения черновика")
    if reason:
        return CreateResumeResult(False, reason=reason)
    if dry_run:
        return CreateResumeResult(True, reason="dry-run; кнопка сохранения не нажата")

    try:
        area_control.fill(area)
        title_control.fill(title)
        submit.click()
        page.wait_for_url(_RESUME_ID_RE, wait_until="commit")
    except PlaywrightError as exc:
        return CreateResumeResult(
            False, reason=f"ошибка после клика сохранения: {exc}", uncertain=True
        )
    match = _RESUME_ID_RE.search(page.url)
    if not match:
        return CreateResumeResult(
            False, reason="новый resume_id не подтверждён после сохранения", uncertain=True
        )
    return CreateResumeResult(True, new_resume_id=match.group(1), reason="черновик создан")

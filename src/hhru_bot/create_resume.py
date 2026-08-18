"""Browser flow for creating an empty resume through the hh.ru UI (#304)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import HH_BASE_URL, RESUMES_FULL_LIST_URL, goto_hh
from .external_forms.detect import normalize
from .selector_groups.resume_list import RESUME_LIST_CARD_TITLE
from .selector_groups.resume_page import (
    RESUME_CREATE_BUTTON,
    RESUME_CREATION_CATEGORY_INPUT,
    RESUME_CREATION_CATEGORY_SEARCH,
    RESUME_CREATION_CATEGORY_SUBMIT,
    RESUME_CREATION_NEXT,
    RESUME_CREATION_POSITION,
    RESUME_CREATION_SELECT_JOB,
    RESUME_CREATION_URL,
)

CREATION_URL = f"{HH_BASE_URL}{RESUME_CREATION_URL}"
_RESUME_ID_RE = re.compile(r"/resume/([0-9a-f]{32,40})(?:[/?#]|$)")


@dataclass
class CreateResumeResult:
    success: bool
    new_resume_id: str = ""
    reason: str = ""
    uncertain: bool = False


def _one(page: Page, selector: str, label: str) -> tuple[Locator | None, str]:
    locator = page.locator(selector)
    count = locator.count()
    if count != 1:
        return None, f"{label} не подтверждён однозначно (совпадений: {count})"
    return locator.first, ""


def _require(locator: Locator | None) -> Locator:
    """Narrow ``_one()``'s optional result after its reason has been checked empty."""
    assert locator is not None
    return locator


def _click_one(page: Page, selector: str, label: str) -> str:
    """Resolve exactly one locator and click it; return a non-empty reason on failure."""
    locator, reason = _one(page, selector, label)
    if reason:
        return reason
    _require(locator).click()
    return ""


def _select_catalog_leaf(page: Page, area: str) -> str:
    """Select one exact leaf from hh.ru's full profession tree."""
    search, reason = _one(page, RESUME_CREATION_CATEGORY_SEARCH, "поиск каталога профессий")
    if reason:
        return reason
    _require(search).fill(area)
    # The filtered tree re-renders asynchronously (React) after typing; .all()
    # right after fill() can observe the stale/blank tree (the same commit-vs-
    # hydration race guarded for SELECT_JOB/POSITION in #304), which would
    # surface as a false "профессия «…» не найдена однозначно (совпадений: 0)".
    page.locator("[data-qa*='tree-selector-item-text-']").first.wait_for(
        state="visible", timeout=15000
    )
    # get_by_text() resolves to the inner ``cell-text-content`` span on the
    # current hh.ru DOM, while the identifier we need is on its wrapper.
    # Match the wrapper by its own rendered text instead of assuming the
    # attribute is attached to the text node.
    candidates = page.locator("[data-qa*='tree-selector-item-text-']").all()
    matches = [
        candidate
        for candidate in candidates
        if normalize(candidate.text_content() or "") == normalize(area)
    ]
    if len(matches) != 1:
        return f"профессия «{area}» не найдена однозначно в каталоге (совпадений: {len(matches)})"
    qa = matches[0].get_attribute("data-qa") or ""
    match = re.search(r"tree-selector-item-text-(\d+)$", qa)
    if not match:
        return f"пункт каталога «{area}» не является leaf-профессией"
    checkbox, reason = _one(
        page,
        RESUME_CREATION_CATEGORY_INPUT.format(match.group(1)),
        f"чекбокс профессии «{area}»",
    )
    if reason:
        return reason
    _require(checkbox).check()
    return _click_one(page, RESUME_CREATION_CATEGORY_SUBMIT, "кнопка каталога профессий")


def _existing_resume_reason(page: Page, title: str) -> str:
    titles = page.locator(RESUME_LIST_CARD_TITLE).all_text_contents()
    if normalize(title) in {normalize(item) for item in titles}:
        return f"резюме с должностью «{title}» уже существует; второе создать нельзя"
    return ""


def create_resume_on_hh(page: Page, *, area: str, title: str, dry_run: bool) -> CreateResumeResult:
    """Create one draft; never uses a direct HTTP request.

    Dry-run only reads the list and wizard DOM.  In particular it never clicks
    the list button, wizard cards, catalog checkboxes, or continue controls.
    """
    goto_hh(page, RESUMES_FULL_LIST_URL)
    duplicate_reason = _existing_resume_reason(page, title)
    if duplicate_reason:
        return CreateResumeResult(False, reason=duplicate_reason)
    create_button, reason = _one(page, RESUME_CREATE_BUTTON, "кнопка создания резюме")
    if reason:
        return CreateResumeResult(False, reason=reason)

    if dry_run:
        goto_hh(page, CREATION_URL)
    else:
        try:
            _require(create_button).click()
            page.wait_for_url(f"**{RESUME_CREATION_URL}**", wait_until="commit")
        except PlaywrightError as exc:
            return CreateResumeResult(False, reason=f"не удалось открыть визард: {exc}")

    # wait_until="commit" only guarantees the URL changed, not that the SPA
    # has hydrated the wizard screen yet (#304 live run: _one() saw count=0
    # on a still-blank body immediately after commit).
    select_job_locator = page.locator(RESUME_CREATION_SELECT_JOB)
    try:
        select_job_locator.first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return CreateResumeResult(False, reason=f"визард не отрисовался: {exc}")

    count = select_job_locator.count()
    if count != 1:
        return CreateResumeResult(
            False,
            reason=f"карточка выбора профессии не подтверждена однозначно (совпадений: {count})",
        )
    select_job = select_job_locator.first
    if dry_run:
        return CreateResumeResult(True, reason="dry-run; визард найден, клики не выполнены")

    try:
        select_job.click()
        page.locator(RESUME_CREATION_POSITION).first.wait_for(state="visible", timeout=15000)
        position, reason = _one(page, RESUME_CREATION_POSITION, "поле поиска профессии")
        if reason:
            return CreateResumeResult(False, reason=reason)
        _require(position).fill(title)
        # The NEXT control (and the catalog screen after SUBMIT below) renders
        # asynchronously after each input; a strict count()/click right away can
        # see count=0 before the SPA hydrates (same #304 race guarded above).
        page.locator(RESUME_CREATION_NEXT).first.wait_for(state="visible", timeout=15000)
        reason = _click_one(page, RESUME_CREATION_NEXT, "кнопка продолжения визарда")
        if reason:
            return CreateResumeResult(False, reason=reason)
        category_reason = _select_catalog_leaf(page, area)
        if category_reason:
            return CreateResumeResult(False, reason=category_reason)
        page.locator(RESUME_CREATION_NEXT).first.wait_for(state="visible", timeout=15000)
        reason = _click_one(page, RESUME_CREATION_NEXT, "кнопка продолжения после каталога")
        if reason:
            return CreateResumeResult(False, reason=reason)
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

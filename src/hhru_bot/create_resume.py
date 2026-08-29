"""Browser flow for creating an empty resume through the hh.ru UI (#304)."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import HH_BASE_URL, RESUMES_FULL_LIST_URL, goto_hh
from .external_forms.detect import normalize
from .selector_groups.resume_list import RESUME_LIST_CARD, RESUME_LIST_CARD_TITLE
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


def _click_one(
    page: Page,
    selector: str,
    label: str,
    *,
    before_click: Callable[[], None] | None = None,
) -> str:
    """Resolve exactly one locator and click it; return a non-empty reason on failure."""
    locator, reason = _one(page, selector, label)
    if reason:
        return reason
    if before_click is not None:
        before_click()
    _require(locator).click()
    return ""


def _select_catalog_leaf(page: Page, area: str, *, filter_timeout: float = 15.0) -> str:
    """Select one exact leaf from hh.ru's full profession tree."""
    # The caller arrives right after clicking the wizard's NEXT control, which
    # re-renders the catalog screen asynchronously (React); a strict _one() on
    # the search input immediately after can observe the stale blank body (the
    # same commit-vs-hydration race guarded for SELECT_JOB/POSITION above).
    try:
        page.locator(RESUME_CREATION_CATEGORY_SEARCH).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return f"экран каталога профессий не отрисовался: {exc}"
    search, reason = _one(page, RESUME_CREATION_CATEGORY_SEARCH, "поиск каталога профессий")
    if reason:
        return reason
    _require(search).fill(area)
    # The filtered tree re-renders asynchronously (React) after typing, and the
    # PRE-filter tree is already populated — so waiting for "a first node" is
    # satisfied instantly by the stale full catalog (живой замер #778: 14 узлов
    # до fill, те же 14 сразу после wait_for, и лишь через ~500 мс остаётся 1).
    # Reading .all() at that moment collects other professions and surfaces as a
    # false "профессия «…» не найдена однозначно (совпадений: 0)". Poll the tree
    # until the exact match appears instead of trusting a single read.
    # get_by_text() resolves to the inner ``cell-text-content`` span on the
    # current hh.ru DOM, while the identifier we need is on its wrapper.
    # Match the wrapper by its own rendered text instead of assuming the
    # attribute is attached to the text node.
    deadline = time.monotonic() + filter_timeout
    matches: list[Locator] = []
    while True:
        candidates = page.locator("[data-qa*='tree-selector-item-text-']").all()
        matches = [
            candidate
            for candidate in candidates
            if normalize(candidate.text_content() or "") == normalize(area)
        ]
        if len(matches) == 1 or time.monotonic() >= deadline:
            break
        page.wait_for_timeout(250)
    if len(matches) != 1:
        return f"профессия «{area}» не найдена однозначно в каталоге (совпадений: {len(matches)})"
    qa = matches[0].get_attribute("data-qa") or ""
    match = re.search(r"tree-selector-item-text-(\d+)$", qa)
    if not match:
        return f"пункт каталога «{area}» не является leaf-профессией"
    # The checkbox shares the tree row confirmed rendered above, but it is still
    # a distinct control the SPA attaches asynchronously; wait before the strict
    # _one() so the commit-vs-hydration pattern stays symmetric across the wizard.
    checkbox_selector = RESUME_CREATION_CATEGORY_INPUT.format(match.group(1))
    try:
        page.locator(checkbox_selector).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return f"чекбокс профессии «{area}» не отрисовался: {exc}"
    checkbox, reason = _one(page, checkbox_selector, f"чекбокс профессии «{area}»")
    if reason:
        return reason
    # ``check()`` по самому <input> не работает: hh.ru прячет его за
    # стилизованной обёрткой (``magritte-checkbox-container``), у input
    # ``tabindex="-1"``, и Playwright падает с «Clicking the checkbox did not
    # change its state» (живой прогон #778). Кликается видимая строка
    # профессии — тот же узел, по которому выше определён leaf.
    matches[0].click()
    if not _require(checkbox).is_checked():
        return f"профессия «{area}» не отмечена после клика по строке каталога"
    return _click_one(page, RESUME_CREATION_CATEGORY_SUBMIT, "кнопка каталога профессий")


def _click_until_screen_switches(
    page: Page,
    card: Locator,
    next_selector: str,
    *,
    attempts: int = 3,
    timeout: int = 7000,
) -> str:
    """Кликать карточку визарда, пока не отрисуется следующий экран.

    ``wait_for(state="visible")`` карточку не страхует: hh.ru отдаёт её
    SSR-разметкой (``<div role="button">``), которая видима сразу, а React
    привязывает обработчик лишь через несколько секунд. Клик в этом окне
    проходит без ошибки и молча не даёт эффекта (живая разведка #778: 3/3
    провала при клике сразу после ``visible``, 3/3 успеха после ожидания
    гидратации).

    Ждать ``__react*`` ключ на элементе было бы прямой проверкой причины, но
    завязало бы код на внутреннее устройство React. Вместо этого проверяется
    наблюдаемый результат — появление следующего экрана. Повтор безопасен:
    карточка выбора профессии ничего не мутирует, а лишний клик по уже
    переключённому экрану невозможен, так как цикл прерывается по первому
    успеху.
    """
    last_error = ""
    for _ in range(attempts):
        card.click()
        try:
            page.locator(next_selector).first.wait_for(state="visible", timeout=timeout)
        except PlaywrightError as exc:
            last_error = str(exc)
            continue
        return ""
    return f"экран визарда не переключился после {attempts} попыток: {last_error}"


def _existing_title_reason(card_count: int, titles: list[str], title: str) -> str:
    """Fail-closed duplicate check from a confirmed card count and read titles.

    ``RESUME_LIST_CARD`` is confirmed; ``RESUME_LIST_CARD_TITLE`` is unconfirmed
    (resume_list.py). A zero-card list is a genuine empty account (the caller
    anchors list hydration on ``RESUME_CREATE_BUTTON`` before reading) and must
    not be blocked: an empty account legitimately creates its first resume.

    For a non-empty list, the safety check is only trustworthy if EVERY confirmed
    card yielded exactly one non-empty title. Any mismatch — fewer titles than
    cards, a blank title, or extra matches — means the title selector drifted and
    an existing same-title resume could be invisible, so refuse rather than
    assume there is no duplicate (Codex cycles 2/3).
    """
    if card_count == 0:
        return ""
    if len(titles) != card_count or not all(titles):
        return "не удалось прочитать заголовки всех существующих резюме; создание запрещено"
    if normalize(title) in {normalize(item) for item in titles}:
        return f"резюме с должностью «{title}» уже существует; второе создать нельзя"
    return ""


def _existing_resume_reason(page: Page, title: str) -> str:
    cards = page.locator(RESUME_LIST_CARD)
    titles = cards.locator(RESUME_LIST_CARD_TITLE).all_text_contents()
    return _existing_title_reason(cards.count(), titles, title)


def create_resume_on_hh(
    page: Page,
    *,
    area: str,
    title: str,
    dry_run: bool,
    before_click: Callable[[], None] | None = None,
) -> CreateResumeResult:
    """Create one draft; never uses a direct HTTP request.

    Dry-run only reads the list and wizard DOM.  In particular it never clicks
    the list button, wizard cards, catalog checkboxes, or continue controls.
    """
    goto_hh(page, RESUMES_FULL_LIST_URL)
    # The duplicate check reads the resume-list DOM; on a just-committed SPA
    # page that list may not be hydrated yet, and an unrendered page would read
    # as "no such title" and wrongly permit creation (fail-open, Codex cycle 2).
    # Anchor hydration on the create button, which the list screen always
    # renders once the SPA has drawn the page — the list itself may legitimately
    # be empty, so it cannot be the anchor. wait_until="commit" is insufficient.
    try:
        page.locator(RESUME_CREATE_BUTTON).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return CreateResumeResult(False, reason=f"список резюме не отрисовался: {exc}")
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

    # Шаги ДО точки невозврата: мутация здесь физически невозможна, поэтому
    # PlaywrightError остаётся обычным failed и не блокирует повтор (#777,
    # тот же принцип, что у before_click-seam в CLAUDE.md, раздел 6).
    try:
        switch_reason = _click_until_screen_switches(page, select_job, RESUME_CREATION_POSITION)
        if switch_reason:
            return CreateResumeResult(False, reason=switch_reason)
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
    except PlaywrightError as exc:
        return CreateResumeResult(False, reason=f"ошибка до сохранения резюме: {exc}")

    # Точка невозврата: клик ниже создаёт резюме, поэтому ЛЮБОЙ сбой начиная
    # отсюда — uncertain (fail-closed, #176): результат клика не наблюдаем.
    try:
        reason = _click_one(
            page,
            RESUME_CREATION_NEXT,
            "кнопка продолжения после каталога",
            before_click=before_click,
        )
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

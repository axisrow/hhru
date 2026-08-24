"""Browser step for irreversible resume deletion (#293).

The operation is intentionally bound to one card containing the requested hash.
No endpoint is called directly: both destructive steps are UI clicks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import RESUMES_FULL_LIST_URL, goto_hh
from .selector_groups.resume_list import (
    RESUME_DELETE_MENU_ACTION,
    RESUME_LIST_ACTION_MORE,
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_LINK_TPL,
)
from .selector_groups.resume_page import (
    RESUME_DELETE_BUTTON,
    RESUME_DELETE_CONFIRM,
    RESUME_DELETE_HIDE_CONFIRM,
)

DELETE_VERIFY_TIMEOUT_MS = 30_000


@dataclass
class DeleteResumeResult:
    resume_id: str
    success: bool
    reason: str
    uncertain: bool = False


def _wait_resume_list_ready(page: Page) -> None:
    """Wait for a positive post-rerender list state.

    The target card detaching is only a transition signal.  During the
    following client render hh.ru can briefly show no cards (or an
    interstitial), so absence at that point is not evidence of deletion.
    A separately resolved list-card marker is required.  There is no
    confirmed empty-state selector in the authenticated DOM research, so an
    empty final list remains uncertain rather than being guessed as ready.
    """
    page.locator(RESUME_LIST_CARD).first.wait_for(
        state="attached", timeout=DELETE_VERIFY_TIMEOUT_MS
    )


def _resolve_delete_action(page: Page, card) -> tuple[Locator | None, str]:
    """Resolve one delete control without losing the card identity boundary.

    Draft cards render ``resume-delete`` inside the card.  A different list
    renderer may put the delete item in a portal after opening the card's
    actions menu, so that item must be searched globally only after the menu
    was opened from this exact card.  Missing or duplicate controls remain a
    normal fail-closed result.
    """
    direct = card.locator(RESUME_DELETE_BUTTON)
    direct_count = direct.count()
    if direct_count > 1:
        return None, "кнопка удаления не подтверждена однозначно"
    if direct_count == 1:
        return direct.first, ""

    more = card.locator(RESUME_LIST_ACTION_MORE)
    if more.count() != 1:
        return None, "меню действий карточки не подтверждено однозначно"

    menu_action = page.locator(RESUME_DELETE_MENU_ACTION)
    try:
        more.first.click()
        menu_action.first.wait_for(state="visible", timeout=15000)
    except PlaywrightError:
        # The SSR actions button can accept a Playwright click before its React
        # handler is hydrated. Reload once and resolve the same card again so
        # the second click is a real menu-open attempt, not a stale locator.
        try:
            page.reload(wait_until="domcontentloaded")
            card.wait_for(state="attached", timeout=DELETE_VERIFY_TIMEOUT_MS)
            if card.count() != 1:
                return None, "карточка для recovery меню не подтверждена однозначно"
            more = card.locator(RESUME_LIST_ACTION_MORE)
            if more.count() != 1:
                return None, "меню действий карточки не подтверждено после recovery"
            more.first.click()
            menu_action = page.locator(RESUME_DELETE_MENU_ACTION)
            menu_action.first.wait_for(state="visible", timeout=15000)
        except PlaywrightError as recovery_exc:
            return None, f"не удалось открыть действие удаления в меню: {recovery_exc}"
    menu_count = menu_action.count()
    if menu_count != 1:
        return None, "действие удаления в меню не подтверждено однозначно"
    return menu_action.first, ""


def delete_resume_on_hh(
    page: Page,
    resume,
    dry_run: bool,
    *,
    before_click: Callable[[], None] | None = None,
) -> DeleteResumeResult:
    """Delete exactly ``resume.resume_id`` after strict identity checks.

    A post-click exception is ``uncertain``: the click may have reached hh.ru.
    Success requires the list URL and disappearance of the identity-bound card.
    """
    resume_id = resume.resume_id
    goto_hh(page, RESUMES_FULL_LIST_URL)
    card_selector = (
        f"{RESUME_LIST_CARD}:has({RESUME_LIST_CARD_LINK_TPL.format(resume_id=resume_id)})"
    )
    card = page.locator(card_selector)
    if card.count() == 0:
        try:
            card.first.wait_for(state="attached", timeout=DELETE_VERIFY_TIMEOUT_MS)
        except PlaywrightError:
            return DeleteResumeResult(
                resume_id,
                False,
                f"карточка resume_id={resume_id} не появилась после загрузки списка",
                False,
            )
    if card.count() != 1:
        return DeleteResumeResult(
            resume_id, False, f"карточка resume_id={resume_id} не подтверждена однозначно", False
        )
    button, action_error = _resolve_delete_action(page, card)
    if action_error:
        return DeleteResumeResult(resume_id, False, action_error, False)
    if dry_run:
        return DeleteResumeResult(resume_id, True, "dry-run; кнопка удаления не нажата")

    for attempt in range(2):
        try:
            # The action can be present in SSR while its React handler is not
            # attached yet.  A bounded pre-click wait plus one safe reload
            # avoids treating that hydration race as a permanent failure.
            button.wait_for(state="visible", timeout=15000)
            button.first.click()
            # The confirm dialog renders asynchronously after the click (React);
            # an immediate count() can observe the DOM before it mounts (same
            # commit-vs-hydration race documented in create_resume.py for #304).
            page.locator(RESUME_DELETE_CONFIRM).first.wait_for(state="visible", timeout=15000)
            break
        except PlaywrightError as exc:
            if attempt == 1:
                return DeleteResumeResult(
                    resume_id, False, f"не удалось открыть подтверждение: {exc}"
                )
            page.reload(wait_until="domcontentloaded")
            # Same commit-vs-hydration guard as above: after domcontentloaded the
            # React card may not be attached yet, so a bare count()==0 right after
            # the reload must not be treated as a permanent failure.
            card = page.locator(card_selector)
            if card.count() == 0:
                try:
                    card.first.wait_for(state="attached", timeout=DELETE_VERIFY_TIMEOUT_MS)
                except PlaywrightError:
                    return DeleteResumeResult(
                        resume_id,
                        False,
                        f"карточка resume_id={resume_id} не появилась после recovery reload",
                        False,
                    )
            if card.count() != 1:
                return DeleteResumeResult(
                    resume_id,
                    False,
                    f"карточка resume_id={resume_id} не подтверждена после recovery reload",
                    False,
                )
            button, action_error = _resolve_delete_action(page, card)
            if action_error:
                return DeleteResumeResult(resume_id, False, action_error, False)

    confirm = page.locator(RESUME_DELETE_CONFIRM)
    if page.locator(RESUME_DELETE_HIDE_CONFIRM).count() > 1 or confirm.count() != 1:
        return DeleteResumeResult(
            resume_id, False, "подтверждение удаления не подтверждено однозначно", False
        )
    try:
        if before_click is not None:
            before_click()
        confirm.first.click()
    except PlaywrightError as exc:
        return DeleteResumeResult(resume_id, False, f"ошибка destructive-клика: {exc}", True)

    # The list URL already matches before the click, so wait_for_url alone is
    # not a post-click signal: Playwright may return immediately.  Waiting for
    # this identity-bound card to detach proves the rendered list changed after
    # the destructive click.  Any verification error stays uncertain because
    # hh.ru may already have accepted the deletion.
    try:
        card.wait_for(state="detached", timeout=DELETE_VERIFY_TIMEOUT_MS)
        # A different card may have existed before the click.  Waiting for
        # that card alone can therefore succeed before the post-delete render
        # settles.  Force a fresh list document so the readiness marker below
        # belongs to state observed after the destructive action.
        page.reload(wait_until="domcontentloaded")
        if not re.fullmatch(r"https://hh\.ru/applicant/my_resumes(?:[/?#].*)?", page.url):
            # hh.ru may redirect a reload to the profile shell.  That route is
            # not a valid post-delete proof; explicitly return to the stable
            # list before deciding whether the destructive action succeeded.
            goto_hh(page, RESUMES_FULL_LIST_URL)
            if not re.fullmatch(r"https://hh\.ru/applicant/my_resumes(?:[/?#].*)?", page.url):
                return DeleteResumeResult(
                    resume_id,
                    False,
                    "удаление не подтверждено: hh.ru не вернул список резюме",
                    True,
                )
        _wait_resume_list_ready(page)
        remaining = page.locator(
            f"{RESUME_LIST_CARD}:has({RESUME_LIST_CARD_LINK_TPL.format(resume_id=resume_id)})"
        ).count()
    except Exception as exc:
        return DeleteResumeResult(
            resume_id, False, f"не удалось проверить результат удаления: {exc}", True
        )
    if remaining:
        return DeleteResumeResult(resume_id, False, "карточка резюме всё ещё отображается", True)
    return DeleteResumeResult(resume_id, True, "резюме удалено; карточка исчезла из списка")

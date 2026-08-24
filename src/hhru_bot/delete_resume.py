"""Browser step for irreversible resume deletion (#293/#573).

The operation is first bound to one list card containing the requested hash.
Published resumes then repeat the identity proof on the exact resume page.
No endpoint is called directly: both destructive steps are UI clicks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import RESUMES_FULL_LIST_URL, goto_hh, open_confirmed_resume
from .selector_groups.resume_list import (
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


def _resolve_profile_delete_action(page: Page, resume_id: str) -> tuple[Locator | None, str]:
    """Resolve deletion on the identity-confirmed resume page.

    The live published-resume menu has visibility/edit/duplicate/share actions
    but no delete action.  Navigating to the exact public resume URL is safe
    only after the list card was already bound to ``resume_id``;
    ``open_confirmed_resume`` independently proves the same identity before
    this resolver can authorize a click.
    """
    try:
        open_confirmed_resume(page, resume_id)
    except (ValueError, PlaywrightError) as exc:
        return None, f"страница целевого резюме не подтверждена: {exc}"

    button = page.locator(RESUME_DELETE_BUTTON)
    button_count = button.count()
    if button_count > 1:
        return None, "кнопка удаления на странице резюме подтверждена неоднозначно"
    try:
        button.first.wait_for(state="visible", timeout=DELETE_VERIFY_TIMEOUT_MS)
    except PlaywrightError as exc:
        return None, f"кнопка удаления на странице резюме не появилась: {exc}"
    if button.count() != 1:
        return None, "кнопка удаления на странице резюме не подтверждена однозначно"
    return button.first, ""


def _resolve_delete_action(page: Page, card, resume_id: str) -> tuple[Locator | None, str, bool]:
    """Resolve an inline draft action or an identity-confirmed profile action."""
    direct = card.locator(RESUME_DELETE_BUTTON)
    direct_count = direct.count()
    if direct_count > 1:
        return None, "кнопка удаления в карточке подтверждена неоднозначно", False
    if direct_count == 1:
        return direct.first, "", False

    button, error = _resolve_profile_delete_action(page, resume_id)
    return button, error, button is not None


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
    button, action_error, profile_fallback_used = _resolve_delete_action(page, card, resume_id)
    if action_error or button is None:
        return DeleteResumeResult(
            resume_id,
            False,
            action_error or "кнопка удаления не подтверждена",
            False,
        )
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
            if profile_fallback_used:
                try:
                    page.reload(wait_until="domcontentloaded")
                except PlaywrightError as reload_exc:
                    return DeleteResumeResult(
                        resume_id,
                        False,
                        f"recovery страницы резюме не завершён: {reload_exc}",
                    )
                button, action_error = _resolve_profile_delete_action(page, resume_id)
                if action_error or button is None:
                    return DeleteResumeResult(
                        resume_id,
                        False,
                        action_error or "кнопка удаления не подтверждена после recovery",
                        False,
                    )
                continue
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
            button, action_error, profile_fallback_used = _resolve_delete_action(
                page, card, resume_id
            )
            if action_error or button is None:
                return DeleteResumeResult(
                    resume_id,
                    False,
                    action_error or "кнопка удаления не подтверждена после recovery",
                    False,
                )

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

    # On the list-card path, detachment is a useful transition signal.  The
    # published-resume profile fallback has no list card in the current DOM;
    # wait for the profile-side confirm control to leave the DOM instead.  The
    # dialog is kept mounted until the profile delete action completes, so this
    # prevents a reload from racing the asynchronous mutation.  The fresh list
    # reload and exact target absence below remain the authoritative proof for
    # both paths.  Any verification error stays uncertain because hh.ru may
    # already have accepted the deletion.
    try:
        if profile_fallback_used:
            page.locator(RESUME_DELETE_CONFIRM).first.wait_for(
                state="detached", timeout=DELETE_VERIFY_TIMEOUT_MS
            )
        else:
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

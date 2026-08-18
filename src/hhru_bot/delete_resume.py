"""Browser step for irreversible resume deletion (#293).

The operation is intentionally bound to one card containing the requested hash.
No endpoint is called directly: both destructive steps are UI clicks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import HH_BASE_URL, goto_hh
from .selector_groups.resume_list import (
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_LINK_TPL,
)
from .selector_groups.resume_page import (
    RESUME_DELETE_BUTTON,
    RESUME_DELETE_CONFIRM,
    RESUME_DELETE_HIDE_CONFIRM,
)

RESUMES_LIST_URL = f"{HH_BASE_URL}/applicant/resumes"


@dataclass
class DeleteResumeResult:
    resume_id: str
    success: bool
    reason: str
    uncertain: bool = False


def delete_resume_on_hh(page: Page, resume, dry_run: bool) -> DeleteResumeResult:
    """Delete exactly ``resume.resume_id`` after strict identity checks.

    A post-click exception is ``uncertain``: the click may have reached hh.ru.
    Success requires the list URL and disappearance of the identity-bound card.
    """
    resume_id = resume.resume_id
    goto_hh(page, RESUMES_LIST_URL)
    card = page.locator(
        f"{RESUME_LIST_CARD}:has({RESUME_LIST_CARD_LINK_TPL.format(resume_id=resume_id)})"
    )
    if card.count() != 1:
        return DeleteResumeResult(
            resume_id, False, f"карточка resume_id={resume_id} не подтверждена однозначно", False
        )
    button = card.locator(RESUME_DELETE_BUTTON)
    if button.count() != 1:
        return DeleteResumeResult(
            resume_id, False, "кнопка удаления не подтверждена однозначно", False
        )
    if dry_run:
        return DeleteResumeResult(resume_id, True, "dry-run; кнопка удаления не нажата")

    try:
        button.first.click()
    except PlaywrightError as exc:
        return DeleteResumeResult(resume_id, False, f"не удалось открыть подтверждение: {exc}")

    confirm = page.locator(RESUME_DELETE_CONFIRM)
    if page.locator(RESUME_DELETE_HIDE_CONFIRM).count() > 1 or confirm.count() != 1:
        return DeleteResumeResult(
            resume_id, False, "подтверждение удаления не подтверждено однозначно", False
        )
    try:
        confirm.first.click()
    except PlaywrightError as exc:
        return DeleteResumeResult(resume_id, False, f"ошибка destructive-клика: {exc}", True)

    # The list URL already matches before the click, so wait_for_url alone is
    # not a post-click signal: Playwright may return immediately.  Waiting for
    # this identity-bound card to detach proves the rendered list changed after
    # the destructive click.  Any verification error stays uncertain because
    # hh.ru may already have accepted the deletion.
    try:
        card.wait_for(state="detached", timeout=30_000)
        if not re.fullmatch(r"https://hh\.ru/applicant/resumes(?:[/?#].*)?", page.url):
            return DeleteResumeResult(
                resume_id, False, "удаление не подтверждено: hh.ru не вернул список резюме", True
            )
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

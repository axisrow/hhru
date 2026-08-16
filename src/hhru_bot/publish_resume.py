"""Публикация черновика резюме через кнопку в живом DOM (#219).

HTTP-контракт намеренно не используется: единственное write-действие здесь —
клик по реально отрисованной кнопке hh.ru после всех проверок.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import HH_BASE_URL, goto_hh, has_login_form
from .config import ResumeConfig
from .responses import NotAuthenticated
from .selector_groups.resume_page import (
    RESUME_PUBLISH_BUTTON,
    RESUME_PUBLISH_BUTTON_DATA_QA,
    RESUME_VISIBILITY_BUTTON,
)

logger = logging.getLogger("hhru_bot.publish_resume")
PUBLISH_TIMEOUT_MS = 30_000
_RESUME_URL_RE = re.compile(r"/resume/([^/?#]+)")


@dataclass
class ResumePublishState:
    status: str | None = None
    is_searchable: bool | None = None
    can_publish_or_update: bool | None = None


@dataclass
class PublishResumeResult:
    resume_id: str
    success: bool
    reason: str = ""
    status: str | None = None
    is_searchable: bool | None = None


def parse_resume_state(markup: str) -> ResumePublishState:
    """Extract the three independent SSR state fields without guessing defaults."""
    state = ResumePublishState()
    for field in ("status", "isSearchable", "canPublishOrUpdate"):
        match = re.search(rf'"{field}"\s*:\s*("(?:[^"\\]|\\.)*"|true|false|null)', markup)
        if not match:
            continue
        raw = match.group(1)
        value = None if raw == "null" else json.loads(raw)
        if field == "status":
            state.status = value
        elif field == "isSearchable":
            state.is_searchable = value
        else:
            state.can_publish_or_update = value
    return state


def _identity_matches(page: Page, resume_id: str) -> bool:
    path = urlsplit(page.url).path.rstrip("/")
    match = _RESUME_URL_RE.fullmatch(path) or _RESUME_URL_RE.search(path)
    return bool(match and match.group(1) == resume_id)


def _visibility_text(page: Page) -> str:
    """Read visibility control text only; never click the visibility control."""
    locator = page.locator(RESUME_VISIBILITY_BUTTON)
    if locator.count() == 1:
        return (locator.first.inner_text() or "").strip()
    return ""


def publish_resume_on_hh(page: Page, resume: ResumeConfig, dry_run: bool) -> PublishResumeResult:
    """Inspect one config resume and optionally click its publish button."""
    url = f"{HH_BASE_URL}/resume/{resume.resume_id}"
    goto_hh(page, url)
    if has_login_form(page):
        raise NotAuthenticated("страница содержит форму входа — сессия отвергнута")
    if not _identity_matches(page, resume.resume_id):
        return PublishResumeResult(resume.id, False, "identity резюме не подтверждён")

    state = parse_resume_state(page.content())
    if state.status is None or state.can_publish_or_update is None:
        return PublishResumeResult(
            resume.id,
            False,
            "состояние резюме не подтверждено (status/canPublishOrUpdate)",
            state.status,
            state.is_searchable,
        )
    if state.status != "not_finished":
        reason = (
            "резюме уже опубликовано" if state.status == "finished" else f"status={state.status}"
        )
        return PublishResumeResult(resume.id, False, reason, state.status, state.is_searchable)
    if state.can_publish_or_update is not True:
        return PublishResumeResult(
            resume.id,
            False,
            f"canPublishOrUpdate={state.can_publish_or_update}; клик запрещён",
            state.status,
            state.is_searchable,
        )

    publish = page.locator(RESUME_PUBLISH_BUTTON).or_(page.locator(RESUME_PUBLISH_BUTTON_DATA_QA))
    count = publish.count()
    if count == 0:
        try:
            publish.first.wait_for(timeout=PUBLISH_TIMEOUT_MS)
            count = publish.count()
        except PlaywrightTimeoutError:
            count = 0
    if count != 1:
        return PublishResumeResult(
            resume.id,
            False,
            "кнопка «Опубликовать» не найдена"
            if count == 0
            else f"кнопка «Опубликовать» определяется неоднозначно ({count}) — клик запрещён",
            state.status,
            state.is_searchable,
        )
    visibility = _visibility_text(page)
    if dry_run:
        reason = "dry-run; кнопка не нажата"
        if visibility:
            reason += f"; видимость: {visibility}"
        return PublishResumeResult(resume.id, True, reason, state.status, state.is_searchable)

    try:
        publish.first.click(timeout=PUBLISH_TIMEOUT_MS)
    except PlaywrightError as exc:
        return PublishResumeResult(
            resume.id,
            False,
            f"ошибка клика; результат не подтверждён: {exc}",
            state.status,
            state.is_searchable,
        )
    # Позитивный сигнал обязателен: после клика ждём, что status больше не draft.
    try:
        page.wait_for_timeout(500)
        after = parse_resume_state(page.content())
    except PlaywrightError as exc:
        return PublishResumeResult(resume.id, False, f"результат публикации не подтверждён: {exc}")
    if after.status != "finished":
        return PublishResumeResult(
            resume.id,
            False,
            "публикация не подтверждена позитивным сигналом",
            after.status,
            after.is_searchable,
        )
    return PublishResumeResult(resume.id, True, "опубликовано", after.status, after.is_searchable)

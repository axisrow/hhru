from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from . import selectors as sel
from .browser import HH_BASE_URL
from .config import ResumeConfig

logger = logging.getLogger("hhru_bot.bump")

BUMP_TIMEOUT_MS = 10_000


@dataclass
class BumpResult:
    resume_id: str
    success: bool
    reason: str = ""


def bump_resume(page: Page, resume: ResumeConfig, dry_run: bool) -> BumpResult:
    url = resume.resume_url if resume.resume_url.startswith("http") else f"{HH_BASE_URL}{resume.resume_url}"
    logger.info("Открываю резюме: %s", url)
    page.goto(url, wait_until="domcontentloaded")

    disabled_hint = page.locator(sel.RESUME_BUMP_DISABLED_HINT)
    if disabled_hint.count() > 0:
        return BumpResult(resume.id, False, "hh.ru сообщает, что поднимать ещё рано")

    bump_button = page.locator(sel.RESUME_BUMP_BUTTON)
    try:
        bump_button.wait_for(timeout=BUMP_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return BumpResult(resume.id, False, "кнопка поднятия резюме не найдена на странице")

    if dry_run:
        logger.info("[DRY-RUN] Поднял бы резюме '%s' в поиске", resume.id)
        return BumpResult(resume.id, True, "dry-run")

    bump_button.click()
    logger.info("Резюме '%s' поднято в поиске", resume.id)
    return BumpResult(resume.id, True, "success")

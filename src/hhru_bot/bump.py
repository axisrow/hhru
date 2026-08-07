from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import selectors as sel
from .browser import HH_BASE_URL, goto_hh
from .config import ResumeConfig

logger = logging.getLogger("hhru_bot.bump")

BUMP_TIMEOUT_MS = 10_000
# Короткий таймаут для опционального disabled-hint (#139): элемент — сигнал
# «поднимать рано», он либо отрисуется быстро, либо детерминированно
# отсутствует (кнопка активна). Ждать полный BUMP_TIMEOUT_MS тут не нужно —
# аналогично OPTIONAL_FIELD_TIMEOUT_MS в apply/steps.py.
BUMP_HINT_TIMEOUT_MS = 1_500


@dataclass
class BumpResult:
    resume_id: str
    success: bool
    reason: str = ""


def bump_resume(page: Page, resume: ResumeConfig, dry_run: bool) -> BumpResult:
    url = (
        resume.resume_url
        if resume.resume_url.startswith("http")
        else f"{HH_BASE_URL}{resume.resume_url}"
    )
    logger.info("Открываю резюме: %s", url)
    goto_hh(page, url)

    # #139: гонка рендера — раньше hint читался сразу через count() > 0, без
    # ожидания. Непрогрузившаяся страница резюме давала 0 совпадений (не
    # «подсказки нет», а «ещё не отрисовалось»), и код шёл жать кнопку поднятия
    # в обход кулдауна hh.ru. Приводим к тому же приёму, что и кнопка ниже:
    # ждём (короткий таймаут — опциональный элемент), ловим PlaywrightTimeoutError
    # как «hint не появился» = легитимное отсутствие.
    disabled_hint = page.locator(sel.RESUME_BUMP_DISABLED_HINT)
    try:
        disabled_hint.wait_for(state="visible", timeout=BUMP_HINT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass
    except PlaywrightError:
        # cycle-review #139: не-timeout ошибка (strict-mode violation и т.п.) —
        # аномалия, а не легитимное «hint нет». Раньше пробрасывалась наружу
        # необработанным traceback вместо fail-closed BumpResult; steps.py
        # (эталон) в этой же ситуации явно возвращает отказ, а не падает.
        return BumpResult(
            resume.id, False, "ошибка при проверке подсказки кулдауна — поднятие отменено"
        )
    else:
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

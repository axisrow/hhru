from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

logger = logging.getLogger("hhru_bot.browser")

HH_BASE_URL = "https://hh.ru"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@contextmanager
def launch_context(storage_state_file: Path, headless: bool = False):
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=headless)
        context_kwargs = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1366, "height": 900},
            "locale": "ru-RU",
        }
        if storage_state_file.exists():
            context_kwargs["storage_state"] = str(storage_state_file)
            logger.info("Загружена сохранённая сессия: %s", storage_state_file)
        else:
            logger.warning(
                "Файл сессии не найден (%s) — потребуется вход в аккаунт", storage_state_file
            )

        context: BrowserContext = browser.new_context(**context_kwargs)
        try:
            yield context
        finally:
            context.close()
            browser.close()


def is_logged_in(page: Page) -> bool:
    page.goto(f"{HH_BASE_URL}/account/login", wait_until="domcontentloaded")
    return "account/login" not in page.url

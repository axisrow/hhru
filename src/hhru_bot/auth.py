from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from .account_profile import read_account_profile
from .browser import (
    GOTO_TIMEOUT_MS,
    HH_BASE_URL,
    goto_hh,
    has_auth_cookie,
    has_login_form,
    launch_browser,
)
from .config import AppConfig
from .session_security import (
    create_storage_state_temp,
    secure_storage_state_parent,
)

logger = logging.getLogger("hhru_bot.auth")


def login(
    config: AppConfig,
    history_path: str | Path = Path("data/history.db"),
    account_dir: str | Path | None = None,
) -> None:
    """
    Открывает hh.ru в headed-браузере и ждёт, пока пользователь вручную войдёт
    в аккаунт (логин/пароль, СМС-код, капча — всё, что попросит hh.ru).
    После подтверждения по состоянию страницы сохраняет сессию
    (cookies + localStorage) в файл, указанный в config.storage_state_file,
    чтобы остальные команды могли переиспользовать вход без повторной
    авторизации.
    """
    poll_interval_seconds = 2
    timeout_seconds = 300
    progress_interval_seconds = 15
    storage_state_file = config.storage_state_file
    secure_storage_state_parent(storage_state_file, account_dir=account_dir)

    with sync_playwright() as p:
        browser = launch_browser(p, headless=False)
        context_kwargs: dict = {
            "viewport": {"width": 1366, "height": 900},
            "locale": "ru-RU",
        }
        # user_agent пробрасывается из account.user_agent; без него — родной UA
        # Playwright (хардкода Chrome/xxx здесь нет, см. #9).
        if config.user_agent:
            context_kwargs["user_agent"] = config.user_agent
        context = browser.new_context(**context_kwargs)
        # #80: context-wide потолок навигации (как в launch_context) — единый
        # источник GOTO_TIMEOUT_MS, вместо хардкода 120000 на самом goto.
        context.set_default_navigation_timeout(GOTO_TIMEOUT_MS)
        # Убираем navigator.webdriver и подделываем window.chrome — без этого
        # hh.ru детектит Playwright и держит кнопку выбора роли disabled. Приём
        # из YAMAKAYAMACO/hh-autoresponder/manual_login.py (рабочий против hh.ru).
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.chrome = {runtime: {}};"
        )
        page = context.new_page()
        goto_hh(page, f"{HH_BASE_URL}/account/login")

        print()
        print("=" * 70)
        print("Откройте вкладку браузера и войдите в свой аккаунт на hh.ru.")
        print("Вход будет подтверждён автоматически после проверки страницы.")
        print("=" * 70)
        started_at = time.monotonic()
        next_progress_at = progress_interval_seconds
        while True:
            elapsed = time.monotonic() - started_at
            if has_auth_cookie(page) and not has_login_form(page):
                break
            if elapsed >= timeout_seconds:
                if not has_auth_cookie(page):
                    logger.warning(
                        "Cookie hhtoken отсутствует — вход, похоже, не завершён. "
                        "Сессия не будет сохранена.",
                    )
                    context.close()
                    browser.close()
                    raise RuntimeError("Cookie hhtoken отсутствует — сессия не сохранена")
                context.close()
                browser.close()
                raise RuntimeError(
                    f"Вход не завершён за {timeout_seconds} секунд, сессия не сохранена",
                )
            if elapsed >= next_progress_at:
                remaining = max(0, timeout_seconds - int(elapsed))
                print(f"[INFO] Ожидание входа... осталось около {remaining} с")
                next_progress_at += progress_interval_seconds
            time.sleep(poll_interval_seconds)

        # Цикл выше выходит сюда только через `break` на строке успеха
        # (has_auth_cookie(page) and not has_login_form(page)) — повторная
        # проверка cookie здесь была бы недостижимым дублированием.
        fd, temporary_state = create_storage_state_temp(storage_state_file, account_dir=account_dir)
        try:
            # Ask Playwright for the state in memory, then serialize it through
            # the already-open 0600 descriptor.  Reopening the temporary path
            # by name would let a writer of a shared custom parent redirect
            # bearer-token contents through a symlink (Codex review).
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(context.storage_state(), handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_state, storage_state_file)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            temporary_state.unlink(missing_ok=True)
            raise
        logger.info("Сессия сохранена: %s", storage_state_file)
        read_account_profile(page, history_path)

        context.close()
        browser.close()

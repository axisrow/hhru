from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from .browser import HH_BASE_URL
from .config import AppConfig

logger = logging.getLogger("hhru_bot.auth")


def login(config: AppConfig) -> None:
    """
    Открывает hh.ru в headed-браузере и ждёт, пока пользователь вручную войдёт
    в аккаунт (логин/пароль, СМС-код, капча — всё, что попросит hh.ru).
    После подтверждения в терминале сохраняет сессию (cookies + localStorage)
    в файл, указанный в config.storage_state_file, чтобы остальные команды
    могли переиспользовать вход без повторной авторизации.
    """
    storage_state_file = config.storage_state_file
    storage_state_file.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context_kwargs: dict = {
            "viewport": {"width": 1366, "height": 900},
            "locale": "ru-RU",
        }
        # user_agent пробрасывается из account.user_agent; без него — родной UA
        # Playwright (хардкода Chrome/xxx здесь нет, см. #9).
        if config.user_agent:
            context_kwargs["user_agent"] = config.user_agent
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto(f"{HH_BASE_URL}/account/login", wait_until="domcontentloaded")

        print()
        print("=" * 70)
        print("Откройте вкладку браузера и войдите в свой аккаунт на hh.ru.")
        print("Когда окажетесь на главной странице (или в личном кабинете),")
        print("вернитесь сюда и нажмите Enter, чтобы сохранить сессию.")
        print("=" * 70)
        input("Нажмите Enter после успешного входа... ")

        current_url = page.url
        if "account/login" in current_url or "hh.ru/account/login" in current_url:
            logger.warning(
                "Похоже, вы всё ещё на странице логина (%s). "
                "Сессия всё равно будет сохранена, но вход мог не завершиться.",
                current_url,
            )

        context.storage_state(path=str(storage_state_file))
        logger.info("Сессия сохранена: %s", storage_state_file)
        print(f"Сессия сохранена в {storage_state_file}")

        context.close()
        browser.close()

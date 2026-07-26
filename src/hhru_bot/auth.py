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
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context_kwargs: dict = {
            "viewport": {"width": 1366, "height": 900},
            "locale": "ru-RU",
        }
        # user_agent пробрасывается из account.user_agent; без него — родной UA
        # Playwright (хардкода Chrome/xxx здесь нет, см. #9).
        if config.user_agent:
            context_kwargs["user_agent"] = config.user_agent
        context = browser.new_context(**context_kwargs)
        # Убираем navigator.webdriver и подделываем window.chrome — без этого
        # hh.ru детектит Playwright и держит кнопку выбора роли disabled. Приём
        # из YAMAKAYAMACO/hh-autoresponder/manual_login.py (рабочий против hh.ru).
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.chrome = {runtime: {}};"
        )
        page = context.new_page()
        page.goto(f"{HH_BASE_URL}/account/login", wait_until="domcontentloaded", timeout=120000)

        print()
        print("=" * 70)
        print("Откройте вкладку браузера и войдите в свой аккаунт на hh.ru.")
        print("Когда окажетесь на главной странице (или в личном кабинете),")
        print("вернитесь сюда и нажмите Enter, чтобы сохранить сессию.")
        print("=" * 70)
        input("Нажмите Enter после успешного входа... ")

        # Реальный маркер залогиненности — cookie hhtoken, а не URL (hh.ru после
        # входа иногда оставляет account/login в реферере/redirect — URL-проверка
        # давала ложный warning при успешном входе).
        cookies = context.cookies()
        has_auth_token = any(c.get("name") == "hhtoken" for c in cookies)
        if not has_auth_token:
            logger.warning(
                "Cookie hhtoken отсутствует — вход, похоже, не завершён. "
                "Сессия всё равно будет сохранена, но отклик/bump могут не сработать.",
            )

        context.storage_state(path=str(storage_state_file))
        logger.info("Сессия сохранена: %s", storage_state_file)

        context.close()
        browser.close()

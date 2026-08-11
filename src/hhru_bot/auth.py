from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from .browser import GOTO_TIMEOUT_MS, HH_BASE_URL, goto_hh, has_auth_cookie
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
        print("Когда окажетесь на главной странице (или в личном кабинете),")
        print("вернитесь сюда и нажмите Enter, чтобы сохранить сессию.")
        print("=" * 70)
        input("Нажмите Enter после успешного входа... ")

        # Реальный маркер залогиненности — cookie hhtoken, а не URL (hh.ru после
        # входа иногда оставляет путь входа в реферере/redirect — URL-проверка
        # давала ложный warning при успешном входе).
        if not has_auth_cookie(page):
            logger.warning(
                "Cookie hhtoken отсутствует — вход, похоже, не завершён. "
                "Сессия не будет сохранена.",
            )
            context.close()
            browser.close()
            raise RuntimeError("Cookie hhtoken отсутствует — сессия не сохранена")

        context.storage_state(path=str(storage_state_file))
        logger.info("Сессия сохранена: %s", storage_state_file)

        context.close()
        browser.close()

"""Общие браузерные примитивы и инвариант подтверждённого состояния.

Пустой результат браузерного пути нельзя выдавать за достоверный, если
состояние страницы не удалось подтвердить: timeout, сетевой сбой, анти-бот или
дрейф селектора требуют ошибки либо явной пометки неопределённости.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("hhru_bot.browser")

HH_BASE_URL = "https://hh.ru"

# Общий словарь состояний браузерной страницы. Эти значения описывают
# подтверждённость результата, а DTO сохраняют исторические bool-поля.
PAGE_STATE = {
    "confirmed": "confirmed",
    "indeterminate": "indeterminate",
    "unreachable": "unreachable",
    "unauthenticated": "unauthenticated",
    "placeholder": "placeholder",
}


class PageStateIndeterminate(RuntimeError):
    """Состояние страницы не подтверждено.

    Пустой результат нельзя выдавать за достоверный, если DOM не подтверждён
    из-за timeout, сетевой ошибки, анти-бота или дрейфа селектора.
    """


# Потолок ожидания навигации по hh.ru. Дефолт Playwright 30с — hh.ru под
# DDoS-Guard/нагрузкой грузится 33с+ (см. #80), и goto падает. Ставим
# context-wide через set_default_navigation_timeout — покрывает ВСЕ goto/
# wait_for_url одним источником (включая двухшаговую навигацию формы отклика,
# CLAUDE.md п.4, #179), без явного timeout в каждом вызове. 90с (не 120с как в
# auth.py раньше): достаточно для медленного hh.ru, но не слепое зависание на
# 1.5 мин при реально упавшем запросе.
GOTO_TIMEOUT_MS = 90_000

# Retry goto: DDoS-Guard регулярно пропускает запрос со 2-й попытки. 3 попытки,
# линейный backoff 2с → 4с.
_GOTO_MAX_ATTEMPTS = 3
_GOTO_BACKOFF_SECONDS = 2.0


def goto_hh(page: Page, url: str, *, ready_selector: str | None = None) -> None:
    """page.goto с retry и готовностью страницы hh.ru (#80).

    Базовый потолок ожидания задаёт context-wide set_default_navigation_timeout
    (GOTO_TIMEOUT_MS), поэтому тут timeout не дублируем — DRY.

    Поведение:
    1. До ``_GOTO_MAX_ATTEMPTS`` попыток goto(wait_until='domcontentloaded').
       Ловим PlaywrightTimeoutError/PlaywrightError — hh.ru под DDoS-Guard
       часто отвечает со 2-й попытки; на последней попытке ошибку пробрасываем
       (как обычный goto без retry).
    2. ``ready_selector`` (опц.) — после удачного goto дождаться конкретного
       ``data-qa`` маркера страницы (короткий GOTO_TIMEOUT_MS, если не задан
       context-timeout), вместо ненадёжного networkidle. None — не ждать.

    domcontentloaded НЕ меняем (рекомендация референсов #80: DDoS-Guard держит
    сеть активной, networkidle может не сработать).
    """
    last_error: Exception | None = None
    for attempt in range(1, _GOTO_MAX_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded")
            if ready_selector:
                page.locator(ready_selector).wait_for(timeout=GOTO_TIMEOUT_MS)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            last_error = exc
            if attempt < _GOTO_MAX_ATTEMPTS:
                wait = _GOTO_BACKOFF_SECONDS * attempt
                logger.warning(
                    "goto не прошёл (попытка %d/%d): %s — повтор через %.1fs",
                    attempt,
                    _GOTO_MAX_ATTEMPTS,
                    exc,
                    wait,
                )
                time.sleep(wait)
    # Последняя попытка провалилась — пробрасываем, как обычный goto.
    assert last_error is not None
    raise last_error


@contextmanager
def launch_context(
    storage_state_file: Path,
    headless: bool = False,
    user_agent: str | None = None,
):
    """Контекст браузера с сохранённой сессией.

    user_agent: None (по умолчанию) — пусть Playwright ставит свой родной UA;
    строка — переопределить UA (если требует hh.ru). Хардкода Chrome/xxx здесь
    намеренно нет.
    """
    with sync_playwright() as p:
        # --disable-blink-features=AutomationControlled убирает главный флаг, по
        # которому hh.ru (DDoS-Guard) держит кнопку входа disabled в Playwright.
        # Приём из YAMAKAYAMACO/hh-autoresponder (рабочий против hh.ru).
        browser: Browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context_kwargs: dict = {
            "viewport": {"width": 1366, "height": 900},
            "locale": "ru-RU",
        }
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        if storage_state_file.exists():
            context_kwargs["storage_state"] = str(storage_state_file)
            logger.info("Загружена сохранённая сессия: %s", storage_state_file)
        else:
            logger.warning(
                "Файл сессии не найден (%s) — потребуется вход в аккаунт", storage_state_file
            )

        context: BrowserContext = browser.new_context(**context_kwargs)
        # #80: потолок навигации context-wide — покрывает ВСЕ goto/wait_for_url
        # единым источником (включая двухшаговую навигацию формы отклика, #179).
        context.set_default_navigation_timeout(GOTO_TIMEOUT_MS)
        # Убираем navigator.webdriver и подделываем window.chrome — без этого
        # hh.ru детектит Playwright и не активирует кнопку входа. Приём из
        # YAMAKAYAMACO/hh-autoresponder/manual_login.py.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.chrome = {runtime: {}};"
        )
        try:
            yield context
        finally:
            context.close()
            browser.close()


def has_auth_cookie(page: Page) -> bool:
    """Проверка авторизации по единственному надёжному маркеру: ``hhtoken``."""
    cookies = page.context.cookies()
    return any(c.get("name") == "hhtoken" for c in cookies)


# Подтверждено анонимным curl-дампом https://hh.ru/account/login (2026-08-12).
# URL не используем: валидная сессия может сохранить /account/login в URL
# после навигации (регрессия #140/#145). Серверная страница входа содержит
# этот маркер формы, авторизованная страница — нет. Намеренно только
# позитивный сигнал: отсутствие формы НЕ подтверждает авторизацию, если
# страница ещё рендерится или селектор устарел.
LOGIN_FORM = "[data-qa='account-login-form']"


def has_login_form(page: Page) -> bool:
    """Отдал ли сервер серверную форму входа (устаревший/отозванный hhtoken).

    ``has_auth_cookie`` подтверждает только наличие cookie в браузерном jar —
    не то, что сервер принял сессию на текущей странице (issue #147: истёкший/
    отозванный токен может остаться в jar без явного Set-Cookie на очистку).
    Комбинация ``has_auth_cookie() and not has_login_form(page)`` — надёжный
    маркер: cookie есть, а сервер не отверг сессию формой входа.
    """
    return page.locator(LOGIN_FORM).count() > 0

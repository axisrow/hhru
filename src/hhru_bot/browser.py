"""Общие браузерные примитивы и инвариант подтверждённого состояния.

Пустой результат браузерного пути нельзя выдавать за достоверный, если
состояние страницы не удалось подтвердить: timeout, сетевой сбой, анти-бот или
дрейф селектора требуют ошибки либо явной пометки неопределённости.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Locator, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("hhru_bot.browser")


class BrowserLaunchError(RuntimeError):
    """Actionable browser-start failure for the CLI/agent boundary."""


def launch_browser(playwright, *, headless: bool) -> Browser:
    """Launch Chromium and classify macOS sandbox failures for the CLI."""
    try:
        return playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
    except PlaywrightError as exc:
        details = str(exc)
        sandbox_markers = (
            "Operation not permitted",
            "Permission denied",
            "Crashpad",
            "NSApplication",
            "NSMenuBarPresentationInstance",
        )
        if any(marker in details for marker in sandbox_markers):
            raise BrowserLaunchError(
                "CODEX_SANDBOX_BROWSER_FAILURE: Chromium was blocked by the "
                "execution sandbox (macOS/Crashpad permission). Retry this same "
                "command with elevated system permission."
            ) from exc
        raise


HH_BASE_URL = "https://hh.ru"
# /applicant/resumes redirects to the profile shell; the dedicated list at
# /applicant/my_resumes is the stable read-only surface used by both
# create_resume.py and delete_resume.py for identity checks and post-action proof.
RESUMES_FULL_LIST_URL = f"{HH_BASE_URL}/applicant/my_resumes"

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


class NotAuthenticated(PageStateIndeterminate):
    """The current page does not prove that the hh.ru session is valid."""


def open_hydrated_resume_editor(
    page: Page,
    *,
    trigger_selector: str | Locator,
    editor_selector: str,
    profile_path: str,
    edit_path: str | re.Pattern[str] | None = None,
    click_trigger: bool = False,
    timeout: int = 30_000,
    trigger_error: str = "кнопка редактирования не подтверждена",
    open_error: str = "форма редактирования не открылась",
    wrong_route_error: str = "форма редактирования открыта не для того резюме",
):
    """Open a resume editor only after its hydrated DOM marker appears.

    Resume pages render edit triggers before React attaches their handlers. A
    click can therefore succeed at the DOM level while doing nothing. Retry
    only while still on the profile page and require the editor marker as the
    positive result; callers may additionally bind the editor to a dedicated
    edit route.
    """
    editor = page.locator(editor_selector)

    # Lightweight unit fakes do not always provide a concrete URL.  Real
    # Playwright pages always expose ``url`` as a string; retain the route
    # guard whenever that production signal is available.
    def current_page_path() -> str | None:
        page_url = page.url
        return urlsplit(page_url).path.rstrip("/") if isinstance(page_url, str) else None

    if click_trigger or editor.count() == 0:
        for attempt in range(2):
            if isinstance(trigger_selector, str):
                trigger = page.locator(trigger_selector)
                if trigger.count() != 1:
                    raise RuntimeError(trigger_error)
            else:
                # Callers passing a row locator have already checked the
                # collection count and selected one row.
                trigger = trigger_selector
            try:
                trigger.click()
                editor.wait_for(state="visible", timeout=timeout)
                break
            except PlaywrightError as exc:
                current_path = current_page_path()
                if attempt or (current_path is not None and current_path != profile_path):
                    raise RuntimeError(open_error) from exc
    expected_path = edit_path or profile_path
    current_path = current_page_path()
    route_matches = current_path is None or (
        bool(expected_path.fullmatch(current_path))
        if isinstance(expected_path, re.Pattern)
        else current_path == expected_path
    )
    if not route_matches:
        raise RuntimeError(wrong_route_error)
    return editor


def require_authenticated_page(
    page: Page,
    *,
    auth_cookie_check=None,
    login_form_check=None,
) -> None:
    """Fail closed when hh.ru did not return an authenticated page.

    This must be called after navigation: the login form is a server-rendered
    positive signal, while a cookie alone only proves that it remains in the jar.
    """
    if not (auth_cookie_check or has_auth_cookie)(page):
        raise NotAuthenticated(
            "cookie hhtoken не найден — сессия истекла (запустите `login`, затем повторите)"
        )
    if (login_form_check or has_login_form)(page):
        raise NotAuthenticated(
            "страница содержит форму входа при наличии hhtoken — сессия отвергнута "
            "сервером (запустите `login`, затем повторите)"
        )


def open_confirmed_resume(page: Page, resume_id: str) -> None:
    """Navigate to and strictly confirm the requested resume identity."""
    if not resume_id:
        raise ValueError("resume_id is required")
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
    require_authenticated_page(page)
    if not resume_identity_matches(page, resume_id):
        raise ValueError("identity резюме не подтверждён")


def resume_identity_matches(page: Page, resume_id: str) -> bool:
    """Return whether the current URL is exactly the requested resume page."""
    path = urlsplit(page.url).path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    return len(parts) >= 2 and parts[-2] == "resume" and parts[-1] == resume_id


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
        browser: Browser = launch_browser(p, headless=headless)
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

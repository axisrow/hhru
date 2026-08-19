"""One-process login by an hh.ru email or SMS code."""

from __future__ import annotations

import logging
import re
import select
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import (
    HH_BASE_URL,
    goto_hh,
    has_auth_cookie,
    has_login_form,
    launch_context,
    require_authenticated_page,
)
from .config import AppConfig
from .cookie_import import write_storage_state
from .selectors import (
    LOGIN_CODE_REQUEST_BUTTON,
    LOGIN_EMAIL_INPUT,
    LOGIN_EMAIL_TYPE,
    LOGIN_PHONE_INPUT,
)

logger = logging.getLogger("hhru_bot.auth_code")

_LOGIN_URL = f"{HH_BASE_URL}/account/login"
_CODE_INPUT = "[data-qa='magritte-pincode-input-field']"
CODE_TIMEOUT_SECONDS = 300
CODE_FORM_TIMEOUT_MS = 15_000
CODE_FILE_POLL_SECONDS = 0.1


def mask_login(value: str) -> str:
    """Return a log-safe representation of an email address or phone number."""
    value = value.strip()
    if "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 7:
        return f"+{digits[:2]}***{digits[-4:]}"
    return "***"


def _read_code(code_file: Path | None, timeout_seconds: int) -> str:
    if code_file is not None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                code = code_file.read_text(encoding="utf-8").strip()
            except OSError:
                code = ""
            if code:
                return code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"--code-file не появился или остался пустым через {timeout_seconds} секунд"
                )
            time.sleep(min(CODE_FILE_POLL_SECONDS, remaining))
    else:
        print(f"[WAIT] Введите код (таймаут {timeout_seconds} сек):", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
        if not ready:
            raise RuntimeError(f"Ввод кода истёк через {timeout_seconds} секунд")
        code = sys.stdin.readline().strip()
    if not code:
        raise ValueError("Код не должен быть пустым")
    return code


def _raise_for_captcha_or_timeout(page) -> None:
    try:
        text = page.locator("body").inner_text().casefold()
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise RuntimeError("Не удалось дождаться ответа hh.ru; вход отменён") from exc
    if "captcha" in text or "капч" in text:
        raise RuntimeError("hh.ru требует капчу; сессия не сохранена")


def _wait_for_one_visible(locator, name: str) -> None:
    """Wait for SPA hydration, then require one unambiguous control."""
    try:
        locator.first.wait_for(state="visible", timeout=CODE_FORM_TIMEOUT_MS)
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise RuntimeError(f"{name} не отрисовался") from exc
    if locator.count() != 1:
        raise RuntimeError(f"{name} не подтверждён")


def _wait_for_authenticated_page(page, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if has_auth_cookie(page) and not has_login_form(page):
                require_authenticated_page(page)
                return
            page.wait_for_timeout(250)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            raise RuntimeError("Ошибка проверки входа; сессия не сохранена") from exc
    raise RuntimeError("hh.ru не подтвердил вход по коду; сессия не сохранена")


def login_with_code(
    config: AppConfig,
    login: str,
    *,
    code_file: Path | None = None,
    timeout_seconds: int = CODE_TIMEOUT_SECONDS,
) -> None:
    """Complete login in one browser process and save only confirmed state."""
    if not login.strip():
        raise ValueError("Логин не должен быть пустым")
    if timeout_seconds <= 0:
        raise ValueError("Таймаут должен быть положительным")
    config.storage_state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_state = config.storage_state_file.with_name(
        config.storage_state_file.name + ".login-code.tmp.json"
    )
    try:
        with launch_context(
            temporary_state, headless=True, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            goto_hh(page, _LOGIN_URL)
            continue_button = page.locator(LOGIN_CODE_REQUEST_BUTTON)
            _wait_for_one_visible(continue_button, "кнопка продолжения login")
            continue_button.click()
            if "@" in login:
                email_type = page.locator(LOGIN_EMAIL_TYPE)
                _wait_for_one_visible(email_type, "переключатель email")
                email_type.check(force=True)
                field = page.locator(LOGIN_EMAIL_INPUT)
            else:
                field = page.locator(LOGIN_PHONE_INPUT)
            _wait_for_one_visible(field, "поле логина")
            field.fill(login)
            page.locator(LOGIN_CODE_REQUEST_BUTTON).click()
            _raise_for_captcha_or_timeout(page)
            code_field = page.locator(_CODE_INPUT)
            _wait_for_one_visible(code_field, "поле одноразового кода")
            print(
                f"[WAIT] Код отправлен на {mask_login(login)}. "
                f"Введите код (таймаут {timeout_seconds} сек):",
                flush=True,
            )
            code = _read_code(code_file, timeout_seconds)
            code_field.fill(code)
            _wait_for_authenticated_page(page, timeout_seconds)
            write_storage_state(context.storage_state(), config.storage_state_file)
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise RuntimeError("Ошибка браузера при входе; сессия не сохранена") from exc
    finally:
        try:
            temporary_state.unlink()
        except FileNotFoundError:
            pass
    logger.info("Вход по одноразовому коду подтверждён; сессия сохранена")

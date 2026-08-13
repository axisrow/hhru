"""Two-phase, non-interactive login by a one-time hh.ru code.

The pending browser state is deliberately separate from the final session.  A
new browser is started for ``submit``; this is useful for callers which cannot
keep a process alive, but depends on hh.ru persisting the challenge in cookies.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import HH_BASE_URL, goto_hh, has_auth_cookie, has_login_form, launch_context
from .config import AppConfig
from .selectors import LOGIN_CODE_REQUEST_BUTTON, LOGIN_EMAIL_INPUT, LOGIN_PHONE_INPUT

logger = logging.getLogger("hhru_bot.auth_code")

_LOGIN_URL = f"{HH_BASE_URL}/account/login"
_PENDING_SUFFIX = ".login-code.pending.json"


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


def _pending_path(config: AppConfig) -> Path:
    return config.storage_state_file.with_name(config.storage_state_file.name + _PENDING_SUFFIX)


def request_code(config: AppConfig, login: str) -> str:
    """Ask hh.ru to send a code and persist the intermediate browser state."""
    if not login.strip():
        raise ValueError("Логин не должен быть пустым")
    pending = _pending_path(config)
    pending.parent.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    try:
        with launch_context(pending, headless=True, user_agent=config.user_agent) as context:
            page = context.new_page()
            goto_hh(page, _LOGIN_URL)
            page.locator(LOGIN_CODE_REQUEST_BUTTON).click()
            field = page.locator(LOGIN_EMAIL_INPUT if "@" in login else LOGIN_PHONE_INPUT)
            if field.count() == 0:
                raise RuntimeError("Не найдено поле логина на странице hh.ru")
            field.fill(login)
            page.locator(LOGIN_CODE_REQUEST_BUTTON).click()
            _raise_for_captcha_or_timeout(page)
            context.storage_state(path=str(pending))
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise RuntimeError(
            "Не удалось запросить код из-за таймаута браузера; повторите --request"
        ) from exc
    logger.info("Запрошен код для %s; промежуточная сессия сохранена", mask_login(login))
    return session_id


def submit_code(config: AppConfig, code: str) -> None:
    """Submit a code using the pending state and save the authenticated state."""
    if not code.strip():
        raise ValueError("Код не должен быть пустым")
    pending = _pending_path(config)
    if not pending.exists():
        raise RuntimeError("Промежуточная сессия не найдена: сначала выполните --request")
    # hh.ru did not expose the code input in the anonymous live dump.  Failing
    # explicitly is safer than guessing a selector and silently timing out.
    raise RuntimeError(
        "Поле одноразового кода не подтверждено анонимным дампом hh.ru; "
        "выполните ручной login (обход капчи запрещён)"
    )


def _raise_for_captcha_or_timeout(page) -> None:
    try:
        text = page.locator("body").inner_text().lower()
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise RuntimeError("Не удалось дождаться ответа hh.ru; повторите --request") from exc
    if "captcha" in text or "капч" in text:
        raise RuntimeError("hh.ru требует капчу; выполните ручной login")


def _confirm_authenticated(page) -> None:
    if not (has_auth_cookie(page) and not has_login_form(page)):
        raise RuntimeError(
            "Вход не подтверждён: cookie hhtoken отсутствует или осталась форма входа"
        )

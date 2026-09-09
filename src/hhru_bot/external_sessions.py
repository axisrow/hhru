"""Сессии внешних провайдеров (Яндекс и т.п.) для внешних форм (#1103).

Отдельный секрет второго уровня: сессия провайдера шире hh.ru (у Яндекса это
почта и пр.), поэтому она НИКОГДА не смешивается с ``hh_session.json`` —
свой файл, свой путь в конфиге (``account.external_sessions.<provider>``).
Вход — только UI в headed-браузере по образцу ``auth.login``: пароль вводит
человек, бот его не видит и не хранит. Никаких прямых API провайдера.

Границы ишью #1103 (осознанные ограничения):
- ya.cc-редиректы автоматически НЕ разворачиваются (анти-фишинг fill-form
  сохраняется); приемлем только явный URL после ручного раскрытия;
- один аккаунт провайдера на аккаунт hh.ru.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from .browser import GOTO_TIMEOUT_MS, launch_browser
from .session_security import (
    create_storage_state_temp,
    secure_storage_state_parent,
)

logger = logging.getLogger("hhru_bot.external_sessions")

# Бюджет ручного входа: как у auth.login — человек вводит логин/пароль/2FA сам.
LOGIN_TIMEOUT_SECONDS = 300
LOGIN_POLL_SECONDS = 2
LOGIN_PROGRESS_SECONDS = 15


@dataclass(frozen=True)
class ExternalProvider:
    """Реестровая запись внешнего провайдера: куда вести и как понять «вошёл».

    ``auth_cookie_names`` — имена cookie, подтверждающих залогиненное состояние
    (проверяются по ИМЕНИ через context.cookies(); значения cookie не читаются
    и не логируются). ``domains`` — домены провайдера, на которых fill-form
    переиспользует сессию автоматически (хост равен домену или поддомен).
    """

    name: str
    login_url: str
    auth_cookie_names: tuple[str, ...]
    domains: tuple[str, ...]


PROVIDERS: dict[str, ExternalProvider] = {
    # Имена auth-cookie Яндекса: sessionid2 — долгоживущая сессия, Session_id —
    # её предшественник; хватит любого. Чтение имени cookie через
    # context.cookies() — это чтение состояния уже открытого контекста, не
    # прямой API-запрос.
    "yandex": ExternalProvider(
        name="yandex",
        login_url="https://login.yandex.ru",
        auth_cookie_names=("sessionid2", "Session_id"),
        domains=("yandex.ru", "yandex.net", "yandex.com", "ya.ru"),
    ),
}


def provider_for_url(url: str) -> ExternalProvider | None:
    """Провайдер, которому принадлежит хост URL (точный домен или поддомен)."""
    host = urlparse(url).netloc.casefold().split("@")[-1].split(":", 1)[0]
    if not host:
        return None
    for provider in PROVIDERS.values():
        if any(host == d or host.endswith("." + d) for d in provider.domains):
            return provider
    return None


def resolve_external_session(
    url: str,
    external_sessions: dict[str, Path],
    forced_provider: str | None = None,
) -> tuple[ExternalProvider, Path] | None:
    """Сессия провайдера для URL или ``None``.

    ``forced_provider`` — явный флаг fill-form (используется даже когда домен
    URL не яндексовый). Файл сессии обязан существовать: ``forced_provider``
    задан, а файла нет — ValueError (вызывающая сторона печатает [FAIL] и
    подсказывает login-external). Автоматический путь (по домену) при
    отсутствующем файле молча возвращает None — fill-form продолжает с
    hh-сессией как до #1103.
    """
    if forced_provider is not None:
        provider = PROVIDERS.get(forced_provider)
        if provider is None:
            raise ValueError(f"Неизвестный провайдер внешней сессии: {forced_provider}")
        path = external_sessions.get(provider.name)
        if path is None or not path.exists():
            raise ValueError(
                f"Файл сессии провайдера '{provider.name}' не найден "
                f"({path if path is not None else 'путь не задан в конфиге'}) — "
                f"выполните login-external --provider {provider.name}"
            )
        return provider, path

    provider = provider_for_url(url)
    if provider is None:
        return None
    path = external_sessions.get(provider.name)
    if path is None or not path.exists():
        return None
    return provider, path


def has_auth_cookie(context, provider: ExternalProvider) -> bool:
    """Залогиненное состояние по ИМЕНИ cookie (значения не читаются)."""
    names = {cookie["name"] for cookie in context.cookies()}
    return any(name in names for name in provider.auth_cookie_names)


def login_external(
    provider_name: str,
    storage_state_file: Path,
    account_dir: str | Path | None = None,
) -> None:
    """Ручной вход во внешнего провайдера и сохранение сессии (#1103).

    По образцу ``auth.login``: открывает страницу входа провайдера в
    headed-браузере, человек сам вводит логин/пароль/2FA (бот их не видит и
    не хранит), бот опрашивает cookie и после подтверждения входа сохраняет
    storage_state в ``storage_state_file`` — отдельный секрет, никогда не
    ``hh_session.json``.
    """
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise ValueError(f"Неизвестный провайдер внешней сессии: {provider_name}")
    secure_storage_state_parent(storage_state_file, account_dir=account_dir)

    with sync_playwright() as p:
        browser = launch_browser(p, headless=False)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="ru-RU",
        )
        context.set_default_navigation_timeout(GOTO_TIMEOUT_MS)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.chrome = {runtime: {}};"
        )
        page = context.new_page()
        page.goto(provider.login_url, wait_until="domcontentloaded")

        print()
        print("=" * 70)
        print(f"Откройте вкладку браузера и войдите в аккаунт ({provider.name}).")
        print("Логин/пароль/2FA вводите сами — бот их не видит и не хранит.")
        print("Вход будет подтверждён автоматически после проверки cookies.")
        print("=" * 70)
        started_at = time.monotonic()
        next_progress_at = LOGIN_PROGRESS_SECONDS
        while True:
            elapsed = time.monotonic() - started_at
            if has_auth_cookie(context, provider):
                break
            if elapsed >= LOGIN_TIMEOUT_SECONDS:
                context.close()
                browser.close()
                raise RuntimeError(
                    f"Вход в {provider.name} не завершён за "
                    f"{LOGIN_TIMEOUT_SECONDS} секунд, сессия не сохранена"
                )
            if elapsed >= next_progress_at:
                remaining = max(0, LOGIN_TIMEOUT_SECONDS - int(elapsed))
                print(f"[INFO] Ожидание входа... осталось около {remaining} с")
                next_progress_at += LOGIN_PROGRESS_SECONDS
            time.sleep(LOGIN_POLL_SECONDS)

        # Тот же безопасный паттерн записи, что в auth.login: state уходит
        # через уже открытый 0600-дескриптор, затем атомарный replace.
        fd, temporary_state = create_storage_state_temp(storage_state_file, account_dir=account_dir)
        try:
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
        logger.info(
            "Сессия внешнего провайдера '%s' сохранена: %s", provider.name, storage_state_file
        )

        context.close()
        browser.close()

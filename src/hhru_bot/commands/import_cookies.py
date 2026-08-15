"""Команда импорта куки hh.ru из Chrome."""

from __future__ import annotations

import argparse
import time
from pathlib import Path


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "import-cookies", help="Импортировать куки hh.ru из профиля Chrome"
    )
    parser.add_argument(
        "--profile",
        type=Path,
        help="Путь к профилю Chrome или имя профиля (Default, Profile 1) — "
        "имя резолвится от стандартного корня профилей Chrome",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    import browser_cookie3

    from ..config import load_config_or_exit
    from ..cookie_import import (
        build_storage_state,
        chrome_cookie_file,
        read_chrome_cookies,
        write_storage_state,
    )

    config = load_config_or_exit(args.config)
    cookie_file = chrome_cookie_file(args.profile)
    try:
        rows = read_chrome_cookies(cookie_file)
        state = build_storage_state(rows)
        now = time.time()
        hhtoken = any(
            cookie["name"] == "hhtoken"
            and cookie["value"]
            and (cookie["expires"] == -1 or cookie["expires"] > now)
            for cookie in state["cookies"]
        )
        if not hhtoken:
            print("[FAIL] Cookie hhtoken не найден; текущая сессия не изменена.")
            return True
        backup = write_storage_state(state, config.storage_state_file)
    except (OSError, RuntimeError, ValueError, browser_cookie3.BrowserCookieError) as exc:
        print(f"[FAIL] Не удалось импортировать куки Chrome: {exc}")
        return True

    print(f"[OK] Импортировано куки hh.ru: {len(state['cookies'])}")
    print("[INFO] hhtoken: найден")
    print(f"[INFO] Сессия записана: {config.storage_state_file}")
    if backup:
        print(f"[INFO] Предыдущая сессия сохранена: {backup}")
    if config.user_agent:
        print("[INFO] account.user_agent уже задан в конфиге.")
    else:
        print(
            "[WARN] account.user_agent не задан. Скопируйте User-Agent из chrome://version "
            "в config.yaml: account.user_agent; без этого hh.ru может отвергнуть сессию."
        )
    print("[INFO] Следующий шаг: запустите list-resumes --remote для проверки сессии.")
    return False

"""Проверка и пересохранение сохранённой сессии hh.ru."""

from __future__ import annotations

import argparse

from ..browser import HH_BASE_URL

SESSION_CHECK_URL = f"{HH_BASE_URL}/applicant/resumes"


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "refresh-token",
        help="Проверить или пересохранить сессию hh.ru (READ/WRITE-local)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Пересохранить подтверждённую сессию в storage_state",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from playwright.sync_api import Error as PlaywrightError

    from ..browser import goto_hh, has_auth_cookie, has_login_form, launch_context
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    try:
        with launch_context(
            config.storage_state_file,
            headless=args.headless,
            user_agent=config.user_agent,
        ) as context:
            page = context.new_page()
            goto_hh(page, SESSION_CHECK_URL)
            if not has_auth_cookie(page):
                print("[FAIL] Сессия недействительна: cookie hhtoken не найден")
                return True
            if has_login_form(page):
                print("[FAIL] Сессия недействительна: hh.ru показал форму входа")
                return True
            if args.force:
                config.storage_state_file.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(config.storage_state_file))
                print(f"[OK] Сессия пересохранена в {config.storage_state_file}")
            else:
                print("[INFO] Сессия действительна, обновление не требуется.")
    except PlaywrightError as exc:
        print(f"[FAIL] Не удалось проверить сессию на hh.ru: {exc}")
        return True
    return False

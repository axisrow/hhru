"""Команда посева кук hh.ru из storage_state в профиль браузера (#1195).

Обратное направление к import-cookies: сессия переносится ФАЙЛОМ в произвольный
профиль браузера (user-data-dir), без единого запроса к hh.ru — воркеру не нужен
одноразовый скрипт с add_cookies (#1186). Интерактивный login это не заменяет.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "session-seed",
        help="Посеять куки hh.ru из storage_state в профиль браузера",
        description=(
            "Переносит сессию hh.ru файлом: читает storage_state аккаунта "
            "(--account как у остальных команд) и сеет куки в указанный каталог "
            "профиля браузера (user-data-dir) через launch_persistent_context + "
            "add_cookies. Без единого запроса к hh.ru — интерактивный login это "
            "не заменяет (#1195). Без hhtoken в storage_state профиль не "
            "мутируется (fail-closed, как import-cookies)."
        ),
    )
    parser.add_argument(
        "profile_dir",
        type=Path,
        help="Каталог профиля браузера (user-data-dir); создаётся при отсутствии",
    )
    parser.set_defaults(func=run)


def _seed_cookies(profile_dir: Path, cookies: list[dict]) -> None:
    """Открыть persistent-контекст профиля и добавить куки. Без навигации."""
    from playwright.sync_api import sync_playwright

    # headless всегда: страница не открывается вовсе, окно пользователю не нужно.
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=True)
        try:
            context.add_cookies(cookies)  # type: ignore[arg-type]
        finally:
            context.close()


def run(args: argparse.Namespace) -> bool:
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    state_file = config.storage_state_file
    if not state_file.exists():
        print(f"[FAIL] Файл сессии не найден: {state_file}")
        return True

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        cookies = state["cookies"]
        if not isinstance(cookies, list):
            raise ValueError("cookies не список")
    # TypeError: валидный JSON, но не объект (список/строка/число) — индексация
    # по строковому ключу; KeyError: ключа cookies нет; ValueError: не список.
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[FAIL] Не удалось прочитать storage_state: {exc}")
        return True

    # Тот же expiry-aware гейт, что у import-cookies: сеять сессию без
    # hhtoken бессмысленно — fail-closed без мутации профиля. Страж
    # isinstance отсекает не-dict элементы списка (иначе .get() уронил бы
    # AttributeError мимо except выше).
    now = time.time()
    has_hhtoken = any(
        isinstance(cookie, dict)
        and cookie.get("name") == "hhtoken"
        and cookie.get("value")
        and (cookie.get("expires", -1) == -1 or cookie.get("expires", -1) > now)
        for cookie in cookies
    )
    if not has_hhtoken:
        print("[FAIL] Cookie hhtoken не найден в storage_state; профиль не изменён.")
        return True

    profile_dir = args.profile_dir.expanduser()
    try:
        _seed_cookies(profile_dir, cookies)
    except Exception as exc:  # noqa: BLE001 — PlaywrightError и ошибки профиля (занят другим Chrome и т.п.)
        print(f"[FAIL] Не удалось посеять куки в профиль: {exc}")
        return True

    print(f"[OK] Куки посеяны в профиль: {len(cookies)}")
    print("[INFO] hhtoken: найден")
    print(f"[INFO] Профиль: {profile_dir}")
    print("[INFO] Следующий шаг: откройте hh.ru в этом профиле и проверьте авторизацию.")
    return False

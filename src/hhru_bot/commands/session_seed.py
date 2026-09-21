"""Команда посева кук hh.ru из storage_state в профиль браузера (#1195).

Обратное направление к import-cookies: сессия переносится ФАЙЛОМ в произвольный
профиль браузера (user-data-dir), без единого запроса к hh.ru — воркеру не нужен
одноразовый скрипт с add_cookies (#1186). Интерактивный login это не заменяет.

Ограничение (#1206): hh.ru привязывает токен к отпечатку клиента — клон кук в
браузер с другим отпечатком сервер может отвергнуть (анонимный session-hhtoken
взамен), при этом сам токен остаётся валидным у исходного клиента. Надёжный
путь для целевого браузера — одноразовый интерактивный вход в нём же.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

# Куки без срока (session в источнике) сеются с этим запасом: workflow
# «посеял → открыл браузер позже» обязан переживать рестарты (#1206).
_SESSION_CLAMP_SECONDS = 30 * 24 * 3600

# Контракт add_cookies (Playwright): остальное — например session из стороннего
# экспорта — отбрасываем сами, не полагаясь на молчаливое поведение драйвера.
_COOKIE_FIELDS = (
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
    "partitionKey",
)


def _normalize_for_seed(cookies: list[dict], now: float) -> list[dict]:
    """Канонический вид для add_cookies: контрактные поля + гарантированный expires.

    Сессионные (expires <= 0) и уже истёкшие куки получают конечный срок:
    Chrome не восстанавливает session-куки чужого запуска, поэтому посев
    «как есть» умирал на первом же рестарте браузера (#1206).
    """
    normalized = []
    for cookie in cookies:
        # Не-dict элемент битого storage_state — не кука; отбрасываем, а не
        # роняем команду (field in 42 → TypeError; класс входа, который гейт
        # hhtoken выше пропускает — dict требуется только для поиска hhtoken).
        if not isinstance(cookie, dict):
            continue
        item = {field: cookie[field] for field in _COOKIE_FIELDS if field in cookie}
        expires = item.get("expires", -1)
        if not isinstance(expires, (int, float)) or expires <= now:
            item["expires"] = now + _SESSION_CLAMP_SECONDS
        normalized.append(item)
    return normalized


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


def _verify_token_survives_restart(profile_dir: Path) -> tuple[bool, str]:
    """Readback-страж (#1206): повторный launch профиля = рестарт из жизни.

    Сеянный hhtoken обязан после нового запуска существовать и быть
    постоянным/неистёкшим — иначе посев тихо не работает, а CLI-сессия при
    этом жива и ошибку не найти.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=True)
        try:
            cookies = {cookie.get("name", ""): cookie for cookie in context.cookies()}
        finally:
            context.close()
    token = cookies.get("hhtoken")
    if token is None:
        return False, "hhtoken отсутствует в профиле после посева"
    if token.get("expires", -1) <= time.time():
        return False, "hhtoken в профиле session/истёк — посев не переживёт рестарт браузера"
    return True, "hhtoken постоянный, переживает рестарт браузера"


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
            "мутируется (fail-closed, как import-cookies). ВАЖНО (#1206): hh.ru "
            "привязывает токен к отпечатку клиента — посев в браузер с другим "
            "отпечатком может быть отвергнут сервером (анонимный session-hhtoken "
            "взамен); для целевого браузера надёжнее одноразовый вход в нём же."
        ),
    )
    parser.add_argument(
        "profile_dir",
        type=Path,
        help="Каталог профиля браузера (user-data-dir); создаётся при отсутствии",
    )
    parser.set_defaults(func=run)


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
    normalized = _normalize_for_seed(cookies, now)
    try:
        _seed_cookies(profile_dir, normalized)
    except Exception as exc:  # noqa: BLE001 — PlaywrightError и ошибки профиля (занят другим Chrome и т.п.)
        print(f"[FAIL] Не удалось посеять куки в профиль: {exc}")
        return True

    try:
        ok, detail = _verify_token_survives_restart(profile_dir)
    except Exception as exc:  # noqa: BLE001 — PlaywrightError и ошибки профиля (занят другим Chrome и т.п.); тот же класс отказов, что и посев
        print(f"[FAIL] Не удалось проверить профиль после посева: {exc}")
        return True
    if not ok:
        print(f"[FAIL] {detail}")
        return True

    print(f"[OK] Куки посеяны в профиль: {len(normalized)}")
    print(f"[INFO] {detail}")
    print(f"[INFO] Профиль: {profile_dir}")
    print("[INFO] Следующий шаг: откройте hh.ru в этом профиле и проверьте авторизацию.")
    return False

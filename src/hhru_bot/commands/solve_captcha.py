"""Ручное решение капчи hh.ru в headful-браузере с сохранением сессии (#1146).

WRITE-local: меняет только storage_state аккаунта, как `login` / `refresh-token
--force`. Анти-бот hh.ru на объёме отдаёт страницы вакансий через `/captcha`
(живой кейс 2026-09-16: рассылка остановлена на 161/200, сайтный флаг — 5
попаданий на 2 разных вакансиях и в headless, и в headful). Существующие
команды решить капчу не позволяют: `login` выходит сразу после
``has_auth_cookie`` (окно живёт секунды, когда сессия уже валидна), dry/probe
пути детектируют отказ и останавливаются.

Команда открывает ЗАЛОГИНЕННОЕ headful-окно на ``--url``, держит его открытым
``--wait-seconds`` — капчу решает только человек; кликов и fill'ов здесь нет.
По истечении окна сессия сохраняется тем же безопасным путём, что и
``refresh-token --force`` (``write_storage_state``: 0600, атомарный replace,
бэкап). Успех — позитивный маркер (fail-closed принцип проекта): текущий URL
без ``/captcha`` плюс auth-cookie. Если капча не решена — сессия НЕ
перезаписывается, печатается ``[FAIL]``.

Никакого обхода анти-бота здесь нет и не должно появляться: hh.ru сам
предлагает «решите её вручную и повторите запуск» — команда даёт этот путь
без одноразовых скриптов.
"""

from __future__ import annotations

import argparse
import time

from ..browser import HH_BASE_URL

# Окно ручного решения: капча + возможная повторная загрузка страницы вакансии.
# 5 минут — тот же порядок, что у login (LOGIN_TIMEOUT_SECONDS), которого
# хватило в живом кейсе #1146; флаг --wait-seconds расширяет для медленных
# капч. Прогресс печатается каждые 30 с.
DEFAULT_WAIT_SECONDS = 300
PROGRESS_INTERVAL_SECONDS = 30

# Контекст несёт hhtoken — куку домена .hh.ru, которую браузер отдаёт ЛЮБОМУ
# поддомену (cycle-review #1147): --url валидируется до запуска браузера, а
# финальный URL — перед сохранением сессии (редиректы/опечатки/фишинг в том же
# залогиненном контексте). Точный хост, без поддоменов.
TRUSTED_SCHEME = "https"
TRUSTED_HOST = "hh.ru"


def _origin_mismatch(url: str) -> bool:
    """Точный доверенный origin: схема https, хост ровно hh.ru, без поддоменов."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.scheme != TRUSTED_SCHEME or parsed.hostname != TRUSTED_HOST


def _target_url_problem(raw: str) -> str | None:
    """Отказ на невалидном --url ДО запуска браузера: схема https, хост hh.ru."""
    if _origin_mismatch(raw):
        return (
            f"--url должен вести на {TRUSTED_SCHEME}://{TRUSTED_HOST} (точный хост, "
            f"без поддоменов), получено: {raw}"
        )
    return None


def _final_url_problem(url: str) -> str | None:
    """Финальная проверка перед сохранением сессии: капча не решена или
    страница ушла с доверенного origin — сессия не перезаписывается."""
    if _captcha_page_still_up(url):
        return f"капча не решена (URL содержит /captcha): {url}"
    if _origin_mismatch(url):
        return f"финальный URL вне доверенного origin {TRUSTED_SCHEME}://{TRUSTED_HOST}: {url}"
    return None


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "solve-captcha",
        help="Ручное решение капчи: headful-окно с сессией, сессия сохраняется (WRITE-local)",
    )
    parser.add_argument(
        "--url",
        default=HH_BASE_URL,
        help=f"Страница, где видна капча (по умолчанию {HH_BASE_URL})",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help=f"Сколько секунд держать окно открытым (по умолчанию {DEFAULT_WAIT_SECONDS})",
    )
    parser.set_defaults(func=run)


def _captcha_page_still_up(url: str) -> bool:
    return "/captcha" in url


def run(args: argparse.Namespace) -> bool:
    from playwright.sync_api import Error as PlaywrightError

    from ..browser import goto_hh, has_auth_cookie, launch_context
    from ..config import load_config_or_exit
    from ..cookie_import import write_storage_state

    config = load_config_or_exit(args.config)
    target_problem = _target_url_problem(args.url)
    if target_problem is not None:
        print(f"[FAIL] {target_problem}")
        return True
    try:
        with launch_context(
            config.storage_state_file,
            headless=False,
            user_agent=config.user_agent,
        ) as context:
            page = context.new_page()
            goto_hh(page, args.url)
            print()
            print("=" * 70)
            print(f"Открыто окно: {args.url}")
            print("РЕШИТЕ КАПЧУ РУКАМИ в открывшемся окне и дождитесь загрузки")
            print("страницы. Кликать за бота не нужно — только капча.")
            print("=" * 70)
            for remaining in range(args.wait_seconds, 0, -PROGRESS_INTERVAL_SECONDS):
                print(
                    f"[INFO] до проверки и сохранения сессии: {remaining} с",
                    flush=True,
                )
                time.sleep(min(PROGRESS_INTERVAL_SECONDS, remaining))

            final_problem = _final_url_problem(page.url)
            if final_problem is not None or not has_auth_cookie(page):
                print(
                    f"[FAIL] {final_problem or 'auth-cookie отсутствует'}; "
                    "сессия НЕ перезаписана — повторите solve-captcha"
                )
                return True
            write_storage_state(
                context.storage_state(),
                config.storage_state_file,
                account_dir=getattr(args, "account_dir", None),
            )
            print(f"[OK] Капча решена, сессия сохранена: {config.storage_state_file}")
    except PlaywrightError as exc:
        print(f"[FAIL] Не удалось открыть окно капчи: {exc}")
        return True
    return False

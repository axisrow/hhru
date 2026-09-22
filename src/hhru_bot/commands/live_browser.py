"""live-browser: браузер live-канала с сессией аккаунта (#1209, этап 2 эпика #588).

READ (навигация стартовой вкладки; никаких действий на hh.ru): запускает
браузер с расширением hhru-live и НАТИВНОЙ загрузкой storage_state аккаунта —
тем же Playwright-клиентом, которым сессия была получена. Это важно (#1206):
hh.ru привязывает токен к отпечатку клиента — клон кук в чужой браузер
(golый CfT-бинар + session-seed) сервер отклоняет, а нативную загрузку
в своём классе клиента принимает (эксперимент P1/P2).

Foreground: Ctrl+C или EOF stdin закрывают браузер, фоновых демонов нет —
как у live-serve. Профиль (user-data-dir) сохраняет куки между запусками;
свежая сессия аккаунта подтягивается при каждом старте из storage_state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_PROFILE_DIR = "data/extension-profile"
DEFAULT_START_URL = "https://hh.ru/applicant/resumes"
_LOGIN_FORM_SELECTOR = "[data-qa='account-login-form']"


def _extension_dir() -> Path:
    """Каталог расширения в репо. Editable install (принятый способ работы):
    src/hhru_bot/commands/live_browser.py → parents[3] — корень репозитория."""
    return Path(__file__).resolve().parents[3] / "extensions" / "hhru-live"


def _launch_args(extension_dir: Path) -> list[str]:
    return [
        f"--load-extension={extension_dir}",
        # Отсекает расширения профиля — грузится только hhru-live. Автозапрет
        # Playwright (--disable-extensions в дефолтных свитчах) этот флаг НЕ
        # подавляет — он гасится ignore_default_args в _launch_context.
        f"--disable-extensions-except={extension_dir}",
        # Chrome 137+ режет --load-extension в branded-сборках; CfT держит
        # переключатель за фичей — как в scripts/run_extension_chrome.sh.
        "--disable-features=DisableLoadExtensionCommandLineSwitch",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def _auth_ok(page: Any) -> bool:
    """True, если на вкладке нет формы входа. После goto SPA может дорисовываться
    («commit не значит отрисовано», CLAUDE.md) — даём короткий бюджетhydration."""
    page.wait_for_timeout(3000)
    return page.locator(_LOGIN_FORM_SELECTOR).count() == 0


def _sync_playwright() -> Any:
    """Точка импорта Playwright: страж conftest запрещает импорт в обычных
    тестах — мокается именно она, а не глобальный модуль."""
    from playwright.sync_api import sync_playwright

    return sync_playwright


def _launch_context(p: Any, profile_dir: Path, headless: bool, extension_dir: Path) -> Any:
    return p.chromium.launch_persistent_context(
        str(profile_dir),
        headless=headless,
        args=_launch_args(extension_dir),
        # Playwright (1.59, chromiumSwitches.js) кладёт --disable-extensions
        # в дефолтные свитчи БЕЗУСЛОВНО — except-флаг в args его не подавляет.
        # Живой прогон 2026-09-22: Chromium-1217 грузил расширение и с обоими
        # флагами (приоритет у except), гасим дефолт явно — поведение не
        # должно зависеть от приоритета двух флагов внутри Chrome.
        ignore_default_args=["--disable-extensions"],
    )


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "live-browser",
        help=(
            "Браузер live-канала: расширение hhru-live + сессия аккаунта "
            "(нативная загрузка storage_state; READ-local, foreground)"
        ),
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(DEFAULT_PROFILE_DIR),
        help=f"Каталог профиля браузера (user-data-dir); по умолчанию {DEFAULT_PROFILE_DIR}",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Без окна (расширение работает и в новом headless)",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_START_URL,
        help=f"Стартовая вкладка (по умолчанию {DEFAULT_START_URL})",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    state_file = config.storage_state_file
    if not state_file.exists():
        print(f"[FAIL] live-browser: файл сессии не найден: {state_file}")
        return True

    extension_dir = _extension_dir()
    if not (extension_dir / "manifest.json").exists():
        print(
            f"[FAIL] live-browser: расширение не найдено: {extension_dir} "
            "(команда рассчитана на editable install из корня репозитория)"
        )
        return True

    sync_playwright = _sync_playwright()

    profile_dir = args.profile_dir.expanduser()
    # Битый storage_state — честный [FAIL], а не сырой traceback (#1209).
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        cookies = state["cookies"]
        if not isinstance(cookies, list):
            raise ValueError("cookies не список")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[FAIL] live-browser: не удалось прочитать storage_state: {exc}", flush=True)
        return True
    with sync_playwright() as p:
        try:
            context = _launch_context(p, profile_dir, args.headless, extension_dir)
        except Exception as exc:  # noqa: BLE001 — профиль занят другим Chrome, нет браузера и т.п.
            print(f"[FAIL] live-browser: не удалось запустить браузер: {exc}", flush=True)
            return True
        try:
            # Нативный формат storage_state = контракт add_cookies; переносим
            # сессию внутри Playwright-клиента — hh.ru принимает его (#1206).
            context.add_cookies(cookies)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — битый storage_state дошли бы до гейта ниже
            print(f"[FAIL] live-browser: не удалось перенести куки сессии: {exc}", flush=True)
            return True
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(args.url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001 — обрыв сети (локальный канал, CLAUDE.md) не гасит браузер
                print(
                    f"[INFO] live-browser: вкладка не открылась ({exc}); браузер работает",
                    flush=True,
                )
            if "hh.ru" in page.url and not _auth_ok(page):
                print(
                    f"[FAIL] live-browser: hh.ru не принял сессию (форма входа на {page.url}) "
                    "— сессия аккаунта недействительна, прогоните login (#1206)",
                    flush=True,
                )
                return True
            print(f"[INFO] live-browser: профиль {profile_dir}, сессия из {state_file}", flush=True)
            print(f"[INFO] вкладка: {page.url}; расширение: {extension_dir}", flush=True)
            # flush обязателен: команда живёт в фоне/под пайпом (чеклист, шаг 0),
            # блочный буфер делает её немой до самого выхода.
            print("[INFO] Ctrl+C или EOF stdin — остановка", flush=True)
            sys.stdin.read()  # foreground, как live-serve: EOF/Ctrl+C — остановка
        except KeyboardInterrupt:
            pass
        finally:
            context.close()
    print("[OK] live-browser остановлен", flush=True)
    return False

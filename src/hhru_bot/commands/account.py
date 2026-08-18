"""Команды управления локальными профилями аккаунтов."""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..accounts import AccountError
from ..report import _ascii_table

DEFAULT_DATA_DIR = Path("data")
DEFAULT_TEMPLATE_PATH = Path("config") / "config.example.yaml"


@dataclass(frozen=True)
class AccountInfo:
    """A configured local account and whether its history exists."""

    name: str
    config_path: Path
    history_exists: bool


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "account",
        help="Управление локальными аккаунтами",
        description="Создание и управление локальными профилями аккаунтов hh.ru.",
    )
    commands = p.add_subparsers(dest="account_command", required=True)

    create = commands.add_parser(
        "create",
        help="Создать локальный аккаунт из шаблона конфигурации",
        description="Создать data/accounts/<name>/ и скопировать туда шаблон конфига.",
    )
    create.add_argument("name", help="Имя нового аккаунта")
    create.set_defaults(func=run_create)

    list_accounts = commands.add_parser(
        "list",
        help="Показать настроенные локальные аккаунты (READ)",
    )
    list_accounts.set_defaults(func=run_list)


def scan_accounts(data_dir: Path = DEFAULT_DATA_DIR) -> list[AccountInfo]:
    """Find account configs below ``data_dir/accounts`` without changing files."""
    accounts_dir = data_dir / "accounts"
    if not accounts_dir.is_dir():
        return []

    result: list[AccountInfo] = []
    for config_path in sorted(accounts_dir.glob("*/config.yaml")):
        if not config_path.is_file():
            continue
        account_dir = config_path.parent
        result.append(
            AccountInfo(
                name=account_dir.name,
                config_path=config_path,
                history_exists=(account_dir / "history.db").is_file(),
            )
        )
    return result


def run_list(args: argparse.Namespace) -> None:
    """Print configured accounts and local history presence."""
    del args
    accounts = scan_accounts()
    if not accounts:
        print("[INFO] Аккаунтов не найдено. Используйте hhru account create <name>.")
        return

    rows = [
        [account.name, str(account.config_path), "да" if account.history_exists else "нет"]
        for account in accounts
    ]
    print(_ascii_table(["name", "config_path", "history_exists"], rows))


def create_account(
    name: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    template_path: Path = DEFAULT_TEMPLATE_PATH,
) -> Path:
    """Create an account directory and copy the shipped config template.

    The directory is created without ``exist_ok`` so a repeated command cannot
    silently overwrite an existing account.
    """
    if not name or name in {".", ".."} or Path(name).name != name:
        raise AccountError(f"недопустимое имя аккаунта: {name!r}")
    if not template_path.is_file():
        raise AccountError(f"шаблон конфигурации не найден: {template_path}")

    account_dir = data_dir / "accounts" / name
    try:
        account_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise AccountError(
            f"аккаунт '{name}' уже существует: {account_dir} (перезапись запрещена)"
        ) from exc

    destination = account_dir / "config.yaml"
    try:
        shutil.copyfile(template_path, destination)
    except OSError:
        # Do not leave a misleading empty account behind when copying fails.
        try:
            destination.unlink(missing_ok=True)
            account_dir.rmdir()
        except OSError:
            # Preserve the original copy error; cleanup is best effort.
            pass
        raise
    return destination


def run_create(args: argparse.Namespace) -> bool:
    """Create an account and return whether the command failed."""
    try:
        config_path = create_account(args.name)
    except (AccountError, OSError) as exc:
        print(f"[FAIL] {exc}")
        return True

    print(
        f'[OK] Аккаунт "{args.name}" создан: {config_path} — '
        "отредактируйте resume_url и параметры перед использованием."
    )
    return False

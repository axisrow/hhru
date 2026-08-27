"""Команды управления локальными профилями аккаунтов."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..accounts import AccountError
from ..report import _ascii_table
from ..session_security import secure_directory

DEFAULT_DATA_DIR = Path("data")
DEFAULT_TEMPLATE_PATH = Path("config") / "config.example.yaml"


@dataclass(frozen=True)
class AccountInfo:
    """A configured local account and its locally observable state."""

    name: str
    config_path: Path
    history_exists: bool
    session_status: str
    last_action: str


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
        history_path = account_dir / "history.db"
        result.append(
            AccountInfo(
                name=account_dir.name,
                config_path=config_path,
                history_exists=history_path.is_file(),
                session_status=_session_status(config_path),
                last_action=_last_action(history_path),
            )
        )
    return result


def _session_status(config_path: Path) -> str:
    """Describe only the locally observable storage-state marker.

    This deliberately reuses ``whoami._check_session``.  The result is not an
    online authentication claim: a local file and cookie can remain after the
    server has revoked a session.
    """
    from yaml import YAMLError

    from ..config import ConfigError, load_config
    from .whoami import _check_session

    try:
        config = load_config(config_path)
    except (AttributeError, ConfigError, OSError, TypeError, ValueError, YAMLError) as exc:
        return f"нет (ошибка конфига: {exc})"

    storage_state = Path(config.storage_state_file)
    try:
        age = _format_file_age(storage_state)
    except OSError:
        age = None
    ok, detail = _check_session(storage_state)
    if not ok:
        if age is not None:
            return f"нет ({detail}; возраст {age})"
        return f"нет ({detail})"
    if age is None:
        return "есть (локальный маркер; возраст неизвестен)"
    return f"есть (локальный маркер; возраст {age})"


def _format_file_age(path: Path, *, now: datetime | None = None) -> str:
    """Return a short human-readable age for a storage-state file."""
    now = now or datetime.now()
    seconds = max(0, int(now.timestamp() - path.stat().st_mtime))
    if seconds < 60:
        return "меньше минуты"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин."
    hours, remainder = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {remainder} мин."
    days, hours = divmod(hours, 24)
    return f"{days} дн. {hours} ч"


def _last_action(history_path: Path) -> str:
    """Return the latest action without creating or migrating ``history.db``."""
    if not history_path.is_file():
        return "—"

    # ``History`` initializes missing schema and therefore cannot be used by
    # this READ command.  SQLite's read-only URI also keeps an existing DB from
    # being changed while account list is inspecting it.
    try:
        uri = f"file:{history_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                """
                SELECT action, status, created_at
                  FROM actions
                 ORDER BY created_at DESC, id DESC
                 LIMIT 1
                """
            ).fetchone()
    except (OSError, sqlite3.Error):
        return "—"
    if row is None:
        return "—"
    action, status, created_at = row
    return f"{action} / {status} / {created_at}"


def run_list(args: argparse.Namespace) -> None:
    """Print configured accounts and locally observable state."""
    del args
    accounts = scan_accounts()
    if not accounts:
        print("[INFO] Аккаунтов не найдено. Используйте hhru account create <name>.")
        return

    rows = [
        [
            account.name,
            str(account.config_path),
            "да" if account.history_exists else "нет",
            account.session_status,
            account.last_action,
        ]
        for account in accounts
    ]
    print(_ascii_table(["name", "config_path", "history_exists", "session", "last_action"], rows))


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
        secure_directory(account_dir, exist_ok=False)
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

"""Команды управления локальными профилями аккаунтов."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..accounts import AccountError, resolve_account_paths, validate_account_name
from ..report import _ascii_table
from ..session_security import secure_directory
from ..write_lock import WriteLockBusy, acquire_write_lock

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

    delete = commands.add_parser(
        "delete",
        help="Удалить локальный аккаунт (план по умолчанию, удаление --force)",
        description=(
            "Показать, что будет удалено в data/accounts/<name>/, "
            "а с --force выполнить необратимое удаление."
        ),
    )
    delete.add_argument("name", help="Имя удаляемого аккаунта")
    delete.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать план и ничего не удалять (это поведение по умолчанию)",
    )
    delete.add_argument(
        "--force",
        action="store_true",
        help="Выполнить удаление (необратимо; уносит конфиг, историю и сессию)",
    )
    delete.set_defaults(func=run_delete)


@dataclass(frozen=True)
class AccountDeletePlan:
    """Everything ``account delete`` would remove, collected read-only."""

    name: str
    account_dir: Path
    config_path: Path
    history_path: Path
    history_records: int | None
    session_path: Path | None
    session_exists: bool


def build_delete_plan(
    name: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> AccountDeletePlan:
    """Collect the deletion plan for a named account without changing files.

    ``resolve_account_paths`` is the single resolution rule: it validates the
    name as a plain path component and fails with ``AccountError`` when the
    account config does not exist.
    """
    paths = resolve_account_paths(name, data_dir=data_dir)
    account_dir = paths.config.parent
    session_path = _session_path(paths.config)
    return AccountDeletePlan(
        name=name,
        account_dir=account_dir,
        config_path=paths.config,
        history_path=paths.history,
        history_records=_count_history_records(paths.history),
        session_path=session_path,
        session_exists=session_path.is_file() if session_path is not None else False,
    )


def _session_path(config_path: Path) -> Path | None:
    """Resolve the account's storage-state file, or None on a broken config."""
    from yaml import YAMLError

    from ..config import ConfigError, load_config

    try:
        config = load_config(config_path)
    except (AttributeError, ConfigError, OSError, TypeError, ValueError, YAMLError):
        return None
    return Path(config.storage_state_file)


def _count_history_records(history_path: Path) -> int | None:
    """Count rows across all tables of ``history.db`` without creating it.

    Read-only URI (the same pattern as ``_last_action``) keeps an existing DB
    from being changed while the plan is built.  ``None`` means the DB is
    missing or unreadable -- the plan still lists the file.
    """
    if not history_path.is_file():
        return None
    try:
        uri = f"file:{history_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            return sum(
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables
            )
    except sqlite3.Error:
        return None


def run_delete(args: argparse.Namespace) -> bool:
    """Delete an account (with ``--force``) or print the plan; return failure."""
    try:
        plan = build_delete_plan(args.name)
    except AccountError as exc:
        print(f"[FAIL] {exc}")
        return True

    print(f'[INFO] План удаления аккаунта "{plan.name}":')
    print(f"  каталог:     {plan.account_dir}")
    print(f"  конфиг:      {plan.config_path}")
    if plan.history_records is None:
        print(f"  history.db:  {plan.history_path} (отсутствует или не читается)")
    else:
        print(f"  history.db:  {plan.history_path} (записей: {plan.history_records})")
    if plan.session_path is None:
        print("  сессия:      файл сессии не определён (ошибка конфига)")
    else:
        state = "есть" if plan.session_exists else "нет"
        # storage_state_file резолвится от директории конфига и легально может
        # указывать за пределы каталога аккаунта; rmtree удалит только сам
        # каталог, и [OK] не должен читаться как "удалено всё из плана".
        if not plan.session_path.resolve().is_relative_to(plan.account_dir.resolve()):
            state += "; вне каталога аккаунта — удалён не будет"
        print(f"  сессия:      {plan.session_path} ({state})")

    if args.dry_run or not args.force:
        print("[DRY-RUN] --force не передан: ничего не удалено.")
        return False

    return _delete_account(plan)


def _delete_account(plan: AccountDeletePlan) -> bool:
    """Remove exactly ``data/accounts/<name>/`` under the account's write lock.

    The non-blocking acquisition is the refusal criterion (#723): a held lock
    means another write command is running against this account right now, and
    removing its state from underneath it is forbidden.  Holding the lock
    through ``rmtree`` also closes the TOCTOU window where a write command
    could start between the check and the deletion.
    """
    # Reuse cli's lock-path formula instead of duplicating the ".hhru.lock"
    # name: the same lock a write command against this account's history would
    # take.  Lazy import -- cli imports this module while building the parser.
    from ..cli import _write_lock_path

    lock_path = _write_lock_path(
        argparse.Namespace(
            command="account",
            config=str(plan.config_path),
            history=str(plan.history_path),
        )
    )
    try:
        with acquire_write_lock(lock_path, command=f"account delete {plan.name}"):
            account_dir = plan.account_dir
            if account_dir.resolve().parent != account_dir.parent.resolve():
                # Defence-in-depth beyond resolve_account_paths: rmtree must
                # only ever run on a direct child of data/accounts.
                print(f"[FAIL] недопустимый путь удаления: {account_dir}")
                return True
            shutil.rmtree(account_dir)
    except WriteLockBusy as exc:
        owner = exc.owner
        detail = (
            f" (pid={owner.get('pid')}, command={owner.get('command')}, "
            f"started_at={owner.get('started_at')})"
            if owner
            else ""
        )
        print(
            f'[FAIL] аккаунт "{plan.name}" не удалён: удерживается write-lock, идёт прогон{detail}'
        )
        return True
    except OSError as exc:
        print(f"[FAIL] {exc}")
        return True

    print(f'[OK] Аккаунт "{plan.name}" удалён: {plan.account_dir}')
    return False


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
    validate_account_name(name)
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

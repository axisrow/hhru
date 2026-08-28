from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from ..config import load_config
from ..diagnostics import _same_path, export_bundle
from ..provenance import RECOVERY_COMMAND, run_doctor
from ..session_security import ACCOUNT_DIR_MODE, SESSION_FILE_MODE, permissions_are_posix
from .account import scan_accounts


@dataclass(frozen=True)
class SessionPermission:
    """Permission facts for one configured local account."""

    name: str
    account_dir: Path
    session_file: Path | None
    account_mode: int | None = None
    session_mode: int | None = None
    error: str | None = None

    @property
    def weak(self) -> bool:
        if self.error or not permissions_are_posix():
            return False
        return bool(
            (self.account_mode is not None and self.account_mode & 0o077)
            or (self.session_mode is not None and self.session_mode & 0o077)
        )


def check_session_permissions(data_dir: Path = Path("data")) -> tuple[SessionPermission, ...]:
    """Inspect every named account without opening or reading session files."""
    result: list[SessionPermission] = []
    for account in scan_accounts(data_dir):
        if not permissions_are_posix():
            result.append(
                SessionPermission(
                    account.name,
                    account.config_path.parent,
                    None,
                    error="Windows ACL не представлены POSIX-режимом",
                )
            )
            continue

        try:
            config = load_config(account.config_path)
            session_file = config.storage_state_file
            account_mode = account.config_path.parent.stat().st_mode & 0o777
            session_mode = session_file.stat().st_mode & 0o777 if session_file.exists() else None
        except Exception:
            # Config errors must not expose arbitrary values from a malformed
            # file, and a diagnostics command must remain useful for the other
            # accounts.
            result.append(
                SessionPermission(
                    account.name,
                    account.config_path.parent,
                    None,
                    error="не удалось прочитать конфигурацию или права",
                )
            )
            continue
        result.append(
            SessionPermission(
                account.name,
                account.config_path.parent,
                session_file,
                account_mode,
                session_mode,
            )
        )
    return tuple(result)


def _format_mode(mode: int | None) -> str:
    return "отсутствует" if mode is None else f"{mode:04o}"


def _print_session_permissions() -> bool:
    entries = check_session_permissions()
    if not entries:
        return False

    weak = False
    if not permissions_are_posix():
        print(
            "[WARN] Проверка прав сессий пропущена: Windows ACL нельзя достоверно "
            "представить POSIX-режимом; проверьте ACL вручную."
        )
    for entry in entries:
        if entry.error:
            print(f"[WARN] [SESSION] {entry.name}: {entry.error}.")
            continue
        session = str(entry.session_file) if entry.session_file else "неизвестен"
        print(
            f"[SESSION] {entry.name}: каталог аккаунта "
            f"{_format_mode(entry.account_mode)}, сессия {session} "
            f"({_format_mode(entry.session_mode)}; ожидается {SESSION_FILE_MODE:04o}), "
            f"ожидаемый каталог {ACCOUNT_DIR_MODE:04o}."
        )
        if entry.weak:
            weak = True
            print(
                f"[WARN] [SESSION] {entry.name}: права слабее ожидаемых. "
                f"Рекомендация: chmod {ACCOUNT_DIR_MODE:03o} {entry.account_dir}; "
                f"chmod {SESSION_FILE_MODE:03o} {session}."
            )
        elif entry.session_mode is None:
            print(f"[INFO] [SESSION] {entry.name}: файл сессии ещё не создан.")
    return weak


def register(subparsers) -> None:
    p = subparsers.add_parser("diagnostics", help="Офлайн диагностика сохранённого command run")
    sub = p.add_subparsers(dest="diagnostics_command", required=True)
    e = sub.add_parser("export", help="Экспорт воспроизводимого incident bundle")
    e.add_argument("--run-id")
    e.add_argument("--output", type=Path)
    e.add_argument("--log", type=Path, default=Path("data/logs/hhru_bot.log"))
    e.add_argument("--dom-dir", type=Path, default=Path("data/logs"))
    e.set_defaults(func=run)
    d = sub.add_parser(
        "doctor",
        help="Проверить установку и права локальных сессий",
        description=(
            "Сравнивает версию, release/tag и commit SHA установленного CLI, "
            "marketplace snapshot и загруженного Codex plugin; проверяет права "
            "каталогов аккаунтов и файлов сессий."
        ),
    )
    d.add_argument(
        "--marketplace-path",
        "--marketplace",
        dest="marketplace",
        type=Path,
        help="Путь к marketplace snapshot (для диагностики нестандартной установки)",
    )
    d.add_argument(
        "--plugin-cache",
        type=Path,
        help="Путь к Codex plugin cache (для диагностики нестандартной установки)",
    )
    d.set_defaults(func=run_doctor_command)


def run(args: argparse.Namespace):
    text = export_bundle(
        history=Path(args.history), run_id=args.run_id, log_path=args.log, dom_dir=args.dom_dir
    )
    if args.output:
        output = args.output.expanduser().resolve()
        history = Path(args.history).expanduser().resolve()
        if any(
            _same_path(output, candidate)
            for candidate in (
                history,
                history.with_name(history.name + "-wal"),
                history.with_name(history.name + "-shm"),
            )
        ):
            raise ValueError("incident bundle нельзя записать поверх history.db")
        if _same_path(output, args.log):
            raise ValueError("incident bundle нельзя записать поверх исходного лога")
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def run_doctor_command(args: argparse.Namespace) -> bool:
    result = run_doctor(marketplace=args.marketplace, plugin_cache=args.plugin_cache)
    for component in result.components:
        print(f"[{component.name}] {component.describe()}")
    permission_drift = _print_session_permissions()
    if not result.drift and not permission_drift:
        print("[OK] CLI, marketplace snapshot и plugin cache согласованы.")
        return False
    if result.drift:
        print("[DRIFT] CLI, marketplace snapshot и plugin cache рассинхронизированы.")
        for reason in result.reasons:
            print(f"[DETAIL] {reason}")
        print(f"[FIX] Выполните одну команду: {RECOVERY_COMMAND}")
    return result.drift or permission_drift

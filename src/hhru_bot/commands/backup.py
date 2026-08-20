"""Backup and restore local account state."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from ..backup import create_backup, restore_backup


def register(subparsers) -> None:
    parser = subparsers.add_parser("backup", help="Создать архив локальных данных")
    parser.add_argument("--output", type=Path, help="Путь к tar.gz")
    parser.set_defaults(func=_backup)

    parser = subparsers.add_parser("restore", help="Восстановить локальные данные из архива")
    parser.add_argument("archive", type=Path, help="Путь к tar/tar.gz")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Выполнить восстановление (без флага — только показать состав)",
    )
    parser.set_defaults(func=_restore)


def _backup(args: argparse.Namespace) -> None:
    output = args.output or Path(args.config).parent / (
        f"backup-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    )
    print(f"[OK] Резервная копия: {create_backup(args.config, args.history, output)}")


def _restore(args: argparse.Namespace) -> None:
    rollback_holder: list[Path] = []
    names = restore_backup(
        args.archive,
        args.config,
        args.history,
        dry_run=not args.apply,
        on_rollback=rollback_holder.append,
    )
    for name in names:
        print(name)
    if rollback_holder:
        print(f"[INFO] Резервная копия перед восстановлением: {rollback_holder[0]}")
    if not args.apply:
        print("[INFO] Сухой прогон: файлы не изменены (для восстановления используйте --apply)")

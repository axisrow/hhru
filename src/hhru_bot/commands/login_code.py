"""Command for one-process login by an hh.ru email or SMS code."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(subparsers) -> None:
    p = subparsers.add_parser("login-code", help="Войти по коду hh.ru в одном процессе")
    p.add_argument("--login", required=True, help="Email или телефон")
    p.add_argument(
        "--code-file",
        type=Path,
        help="Файл с одноразовым кодом; без него код читается из stdin",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..auth_code import login_with_code
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    login_with_code(
        config,
        args.login,
        code_file=args.code_file,
        account_dir=getattr(args, "account_dir", None),
    )
    print("[OK] Сессия сохранена")

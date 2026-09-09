"""Команда login-external: ручной вход во внешнего провайдера (#1103)."""

from __future__ import annotations

import argparse


def register(subparsers) -> None:
    from ..external_sessions import PROVIDERS

    p = subparsers.add_parser(
        "login-external",
        help="Вручную войти во внешнего провайдера (Яндекс) и сохранить сессию",
    )
    p.add_argument(
        "--provider",
        required=True,
        choices=sorted(PROVIDERS),
        help="Провайдер внешней сессии",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..config import load_config_or_exit
    from ..external_sessions import login_external

    config = load_config_or_exit(args.config)
    storage_state_file = config.external_sessions.get(args.provider)
    if storage_state_file is None:
        print(
            f"[FAIL] В конфиге не задан путь сессии провайдера '{args.provider}' "
            f"(account.external_sessions.{args.provider}.storage_state_file)"
        )
        return
    login_external(
        args.provider,
        storage_state_file,
        account_dir=getattr(args, "account_dir", None),
    )
    print(f"[OK] Сессия провайдера '{args.provider}' сохранена")

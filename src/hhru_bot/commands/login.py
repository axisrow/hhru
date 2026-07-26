"""Команда login: ручной вход и сохранение сессии hh.ru."""

from __future__ import annotations

import argparse


def register(subparsers) -> None:
    p = subparsers.add_parser("login", help="Войти в аккаунт hh.ru и сохранить сессию")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..auth import login
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    login(config)

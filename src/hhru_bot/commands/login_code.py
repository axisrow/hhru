"""Команда login-code: запрос и отправка одноразового кода hh.ru."""

from __future__ import annotations

import argparse


def register(subparsers) -> None:
    p = subparsers.add_parser("login-code", help="Неинтерактивный вход по одноразовому коду hh.ru")
    phase = p.add_mutually_exclusive_group(required=True)
    phase.add_argument("--request", action="store_true", help="Запросить код")
    phase.add_argument("--submit", action="store_true", help="Отправить код")
    p.add_argument("--login", help="Email или телефон (только с --request)")
    p.add_argument("--code", help="Одноразовый код (только с --submit)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..auth_code import request_code, submit_code
    from ..config import load_config_or_exit

    if args.request == (args.login is None) or args.submit == (args.code is None):
        raise ValueError("Для --request нужен --login, для --submit нужен --code")
    config = load_config_or_exit(args.config)
    if args.request:
        session_id = request_code(config, args.login)
        print(f"[OK] Код запрошен; идентификатор сессии: {session_id}")
    else:
        submit_code(config, args.code)
        print("[OK] Сессия сохранена")

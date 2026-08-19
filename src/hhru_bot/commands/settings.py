"""Управление локальными настройками в history.db."""

from __future__ import annotations

import argparse

from ..report import _ascii_table


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "settings",
        help="Показать или установить локальные настройки",
        description="Показать все настройки, получить значение или установить ключ.",
    )
    parser.add_argument("key", nargs="?", help="Ключ настройки")
    parser.add_argument("value", nargs="?", help="Новое значение настройки")
    parser.set_defaults(func=run)


def _value_type(value: str) -> str:
    return "bool" if value.lower() in {"true", "false"} else "str"


def run(args: argparse.Namespace) -> None:
    from ..history import History

    history = History(args.history)
    if args.key is None:
        rows = history.list_settings()
        print(
            _ascii_table(
                ["Тип", "Ключ", "Значение"],
                [[_value_type(row["value"]), row["key"], row["value"]] for row in rows],
            )
        )
    elif args.value is None:
        value = history.get_setting(args.key)
        if value is not None:
            print(value)
    else:
        history.set_setting(args.key, args.value)
        print("[OK]")

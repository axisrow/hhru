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
    from ._common import ApplyProgress, run_supervised_command

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
            print(f'[INFO] настройка "{args.key}" не найдена')
    else:

        def _set_setting(progress: ApplyProgress) -> bool:
            progress.begin_attempt()
            history.set_setting(args.key, args.value)
            progress.applied_count += 1
            print("[OK]")
            return False

        # Keep settings writes in the same SQLite lease as other durable
        # commands.  This matters when --config and --history point at
        # different roots: copy-resume's fcntl lock then cannot protect this
        # direct history.db mutation by itself.
        return run_supervised_command(
            command="settings",
            history=history,
            requested_limit=1,
            body=_set_setting,
            print_summary=False,
        )

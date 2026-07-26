"""Точка входа CLI: skeleton build_parser с авторегистрацией команд + main().

Команды живут в пакете commands/, каждый модуль реализует register(subparsers).
build_parser обходит их через pkgutil.iter_modules и вызывает register — добавление
команды не требует правок этого файла.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys

from . import commands as _commands_pkg
from .config import PROJECT_ROOT
from .logging_setup import setup_logging

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "data" / "history.db"


def register_commands(subparsers: argparse._SubParsersAction) -> list[str]:
    """Обходит команды/ и вызывает register() у каждого модуля. Возвращает имена команд."""
    registered: list[str] = []
    for module_info in pkgutil.iter_modules(_commands_pkg.__path__):
        name = module_info.name
        if name.startswith("_"):
            continue
        module = importlib.import_module(f"{_commands_pkg.__name__}.{name}")
        if not hasattr(module, "register"):
            continue
        module.register(subparsers)
        registered.append(name)
    return registered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hhru_bot",
        description="Автоматизация поиска, откликов и поднятия резюме на hh.ru",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Путь к config.yaml")
    parser.add_argument(
        "--history", default=str(DEFAULT_HISTORY_PATH), help="Путь к файлу истории (SQLite)"
    )
    parser.add_argument(
        "--headless", action="store_true", help="Запустить браузер в headless-режиме"
    )
    parser.add_argument("--verbose", action="store_true", help="Подробное логирование")

    subparsers = parser.add_subparsers(dest="command", required=True)
    register_commands(subparsers)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(130)


if __name__ == "__main__":
    main()

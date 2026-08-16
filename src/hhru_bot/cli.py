"""Точка входа CLI: skeleton build_parser с авторегистрацией команд + main().

Команды живут в пакете commands/, каждый модуль реализует register(subparsers).
build_parser обходит их через pkgutil.iter_modules и вызывает register — добавление
команды не требует правок этого файла.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import pkgutil
import sys
from pathlib import Path

from . import commands as _commands_pkg
from .logging_setup import setup_logging

# Дефолтные пути — ОТНОСИТЕЛЬНЫЕ (relative-to-cwd), а не привязанные к пакету.
# После `pip install` пакет уезжает в site-packages, и привязка путей к
# расположению кода (как раньше через PROJECT_ROOT = parents[2]) ломала бы поиск
# data/config.yaml. Относительные пути Python резолвит от cwd в рантайме —
# пользователь запускает `hhru-bot` из директории проекта, где рядом лежит
# data/. Относительные строки также стабильно смотрятся в --help и в
# автоген-справочнике README (gen_cli_docs.py), не завися от машины.
#
# Все изменяемые данные — под data/ (#133): конфиг, БД, сессия, логи. Вся папка
# целиком в .gitignore одной строкой.
DEFAULT_CONFIG_PATH = Path("data") / "config.yaml"
DEFAULT_HISTORY_PATH = Path("data") / "history.db"


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

    # READ-команда `log` намеренно минует setup_logging: FileHandler создал бы
    # data/logs/hhru_bot.log на запись до run(), что нарушает READ-контракт «не меняет
    # локально» (#21), делает ветку «файл не найден» недостижимой (setup_logging
    # создаёт пустой лог) и падает PermissionError в read-only-директории.
    # log сам ничего не логирует — ему не нужны handlers (цикл ревью #61, #58).
    if args.command != "log":
        setup_logging(verbose=args.verbose)

    try:
        failed = args.func(args)
        # Fail-closed contract (#148) is opt-in: only commands that report a
        # real bool success flag (search/apply/run) can trip sys.exit(1).
        # Commands returning other truthy values (e.g. clear-skipped's int
        # deleted-row count) or None must not be mistaken for a failure.
        if failed is True:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(130)
    except SystemExit:
        # sys.exit() из самой команды (напр. load_config_or_exit) — не наш случай,
        # пробрасываем как есть, не логируем как крах.
        raise
    except Exception:
        # #179: раньше необработанное исключение из args.func (напр. Playwright
        # TimeoutError, не пойманный внутри pipeline) печаталось Python'ом только
        # в stderr — traceback не попадал в data/logs/hhru_bot.log, хотя
        # setup_logging() уже успел настроить FileHandler на этот момент.
        # logger.exception пишет полный traceback туда же, куда обычные логи.
        if args.command != "log":
            logging.getLogger("hhru_bot").exception(
                "Необработанное исключение в команде '%s'", args.command
            )
        raise


if __name__ == "__main__":
    main()

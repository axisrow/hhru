"""Команда run: полный цикл apply + bump для указанных резюме."""

from __future__ import annotations

import argparse

from . import apply as apply_cmd
from . import bump as bump_cmd
from ._common import add_common_args


def register(subparsers) -> None:
    p = subparsers.add_parser("run", help="Полный цикл: apply + bump для указанных резюме")
    add_common_args(p)
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Максимум откликов за запуск (0 = без ограничения кроме дневного лимита)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    print("=== Полный цикл: search -> apply -> bump ===")
    apply_cmd.run(args)
    bump_cmd.run(args)

"""Команда run: полный цикл apply + bump для указанных резюме."""

from __future__ import annotations

import argparse

from . import apply as apply_cmd
from . import bump as bump_cmd
from ._common import add_common_args, add_force_arg


def register(subparsers) -> None:
    p = subparsers.add_parser("run", help="Полный цикл: apply + bump для указанных резюме")
    add_common_args(p)
    # apply_cmd.run(args) reuses this Namespace — apply's --force must exist here.
    add_force_arg(p)
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Максимум откликов за запуск (0 = без ограничения кроме дневного лимита)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    print("=== Полный цикл: search -> apply -> bump ===")
    apply_failed = apply_cmd.run(args)
    # Apply and bump are independent actions: even if vacancy search is
    # indeterminate, bump still has a valid, unrelated resume-page operation.
    # Keep bump for the same resume and report apply's failure to the caller.
    bump_cmd.run(args)
    return bool(apply_failed)

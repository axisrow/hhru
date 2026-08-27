"""Команда run: полный цикл apply + bump для указанных резюме."""

from __future__ import annotations

import argparse

from ..exit_codes import CommandExitCode
from . import apply as apply_cmd
from . import bump as bump_cmd
from ._common import (
    add_common_args,
    add_force_arg,
    add_learn_questionnaires_arg,
    add_limit_arg,
)


def register(subparsers) -> None:
    p = subparsers.add_parser("run", help="Полный цикл: apply + bump для указанных резюме")
    add_common_args(p, max_pages_default=None)
    # apply_cmd.run(args) reuses this Namespace — apply's --force must exist here.
    add_force_arg(p)
    add_learn_questionnaires_arg(p)
    add_limit_arg(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool | CommandExitCode:
    print("=== Полный цикл: search -> apply -> bump ===")
    apply_failed = apply_cmd.run(args)
    # Apply and bump are independent actions: even if vacancy search is
    # indeterminate, bump still has a valid, unrelated resume-page operation.
    # Keep bump for the same resume and report apply's failure to the caller.
    if isinstance(apply_failed, CommandExitCode):
        return apply_failed
    bump_failed = bump_cmd.run(args)
    if isinstance(bump_failed, CommandExitCode):
        return bump_failed
    return bool(apply_failed or bump_failed)

"""Команда apply: поиск и отклик на подходящие вакансии с троттлингом."""

from __future__ import annotations

import argparse

from ._common import add_common_args, resumes_from_args, run_apply_for_resume


def register(subparsers) -> None:
    p = subparsers.add_parser("apply", help="Найти и откликнуться на подходящие вакансии")
    add_common_args(p)
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Максимум откликов за запуск (0 = без ограничения кроме дневного лимита)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import Throttle

    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = resumes_from_args(config, args)
    throttle = Throttle(config.throttle, history)

    with launch_context(config.storage_state_file, headless=args.headless) as context:
        page = context.new_page()
        for resume in resumes:
            run_apply_for_resume(page, config, resume, history, throttle, args)

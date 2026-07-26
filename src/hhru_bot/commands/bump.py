"""Команда bump: поднять резюме в поиске (кулдаун 4ч, дневной лимит)."""

from __future__ import annotations

import argparse

from ._common import add_common_args, resumes_from_args


def register(subparsers) -> None:
    p = subparsers.add_parser("bump", help="Поднять резюме в поиске")
    add_common_args(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..bump import bump_resume
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import LimitReached, Throttle

    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = resumes_from_args(config, args)
    throttle = Throttle(config.throttle, history)

    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        for resume in resumes:
            print(f"\n=== Поднятие резюме: {resume.id} ===")

            try:
                throttle.check_bump_limit(resume.resume_id, args.dry_run)
            except LimitReached as e:
                print(f"Пропуск: {e}")
                continue

            can_bump, wait_left = throttle.can_bump_now(resume.resume_id)
            if not can_bump:
                print(f"Пропуск: рано поднимать, подождите ещё {wait_left}")
                continue

            result = bump_resume(page, resume, args.dry_run)
            status = "dry_run" if args.dry_run else ("success" if result.success else "failed")
            # Для action='bump' нет естественного vacancy_id (поднятие резюме, не отклик).
            # actions.vacancy_id NOT NULL — заполняем resume.resume_id как sentinel; UNIQUE-индекс
            # idx_resume_vacancy_apply существует только WHERE action='apply', так что коллизий нет.
            history.record_action(resume.resume_id, resume.resume_id, "bump", status, result.reason)

            if result.success:
                print(f"  [OK] {resume.id} поднято")
            else:
                print(f"  [FAIL] {resume.id} — {result.reason}")

            throttle.wait(f"после поднятия резюме '{resume.id}'")

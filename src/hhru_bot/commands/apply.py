"""Команда apply: поиск и отклик на подходящие вакансии с троттлингом."""

from __future__ import annotations

import argparse

from ._common import (
    _build_scoring_provider,
    add_common_args,
    add_force_arg,
    resumes_from_args,
    run_apply_for_resume,
)


def register(subparsers) -> None:
    p = subparsers.add_parser("apply", help="Найти и откликнуться на подходящие вакансии")
    add_common_args(p)
    add_force_arg(p)
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Максимум откликов за запуск (0 = без ограничения кроме дневного лимита)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import Throttle

    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = resumes_from_args(config, args)
    throttle = Throttle(config.throttle, history)

    failed = False
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        if len(resumes) > 1:
            from ..apply.router import merge_vacancies, route_vacancies
            from ..search import VacancySearchIndeterminate, search_vacancies

            feeds = []
            for resume in resumes:
                try:
                    feeds.append(
                        (resume, search_vacancies(page, resume.search, max_pages=args.max_pages))
                    )
                except VacancySearchIndeterminate as e:
                    print(f"[FAIL] {e}")
                    failed = True
            merged = merge_vacancies(feeds)
            providers = {r.id: _build_scoring_provider(config, r) for r in resumes}
            routed = route_vacancies(
                merged,
                resumes,
                history,
                scoring_providers=providers,
            )
            cards_by_resume = {
                resume.id: [
                    item.card
                    for item in merged
                    if routed.get(item.card.vacancy_id, None)
                    and routed[item.card.vacancy_id].resume is resume
                ]
                for resume in resumes
            }
        else:
            cards_by_resume = None
        for resume in resumes:
            if cards_by_resume is None:
                result = run_apply_for_resume(page, config, resume, history, throttle, args)
            else:
                result = run_apply_for_resume(
                    page,
                    config,
                    resume,
                    history,
                    throttle,
                    args,
                    cards_by_resume[resume.id],
                )
            failed = result or failed
    return failed

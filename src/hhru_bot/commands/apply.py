"""Команда apply: поиск и отклик на подходящие вакансии с троттлингом."""

from __future__ import annotations

import argparse

from ._common import (
    ApplyRunStopped,
    _build_scoring_provider,
    add_common_args,
    add_force_arg,
    add_limit_arg,
    apply_search_page_limit,
    resumes_from_args,
    run_apply_for_resume,
)


def register(subparsers) -> None:
    p = subparsers.add_parser("apply", help="Найти и откликнуться на подходящие вакансии")
    add_common_args(p, max_pages_default=None)
    add_force_arg(p)
    add_limit_arg(p)
    p.add_argument(
        "--approved", type=int, metavar="ID", help="Отправить ровно approved-запись review-очереди"
    )
    p.add_argument("--permit", help="Одноразовый permit из `review approve`")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import LimitReached, Throttle

    if getattr(args, "approved", None) is not None and args.dry_run:
        print("[FAIL] --approved нельзя использовать вместе с --dry-run")
        return True

    config = load_config_or_exit(args.config)
    history = History(args.history)
    if getattr(args, "approved", None) is not None and args.resume is None:
        items = [item for item in history.review_items() if item["id"] == args.approved]
        matches = [
            resume
            for resume in config.resumes
            if items and resume.resume_id == items[0]["resume_id"]
        ]
        if not matches:
            print("[FAIL] approved-запись не связана с резюме из текущего конфига")
            return True
        args.resume = matches[0].id
    resumes = resumes_from_args(config, args)
    throttle = Throttle(config.throttle, history)

    try:
        throttle.check_apply_limit(resumes[0].resume_id if resumes else "", args.dry_run)
    except LimitReached as e:
        # The apply limit is account-wide; check it before opening a browser or
        # paying for cross-resume search/scoring.
        print(f"Пропуск: {e}")
        return False

    failed = False
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        if len(resumes) > 1:
            from ..apply.antibot import raise_for_antibot
            from ..apply.router import merge_vacancies, route_vacancies
            from ..search import VacancySearchIndeterminate, search_vacancies

            routing_resumes = resumes
            if not args.dry_run:
                from ..copy_resume import resolve_numeric_resume_ids

                ids_by_hash = resolve_numeric_resume_ids(page)
                raise_for_antibot(page)
                if ids_by_hash is not None:
                    ready = {
                        resume.resume_id
                        for resume in resumes
                        if getattr(ids_by_hash, "statuses", {}).get(resume.resume_id)
                        not in (None, "not_finished")
                    }
                    routing_resumes = [r for r in resumes if r.resume_id in ready]
            feeds = []
            for resume in routing_resumes:
                try:
                    feeds.append(
                        (
                            resume,
                            search_vacancies(
                                page,
                                resume.search,
                                max_pages=apply_search_page_limit(args),
                            ),
                        )
                    )
                except VacancySearchIndeterminate as e:
                    raise_for_antibot(page)
                    print(f"[FAIL] {e}")
                    failed = True
                raise_for_antibot(page)
            merged = merge_vacancies(feeds)
            providers = {r.id: _build_scoring_provider(config, r) for r in routing_resumes}
            routed = route_vacancies(
                merged,
                routing_resumes,
                history,
                scoring_providers=providers,
            )
            cards_by_resume = {
                resume.id: sorted(
                    [
                        item.card
                        for item in merged
                        if routed.get(item.card.vacancy_id, None)
                        and routed[item.card.vacancy_id].resume is resume
                    ],
                    key=lambda card: -routed[card.vacancy_id].score,
                )
                for resume in resumes
            }
            ranked_by_resume = {
                resume.id: sorted(
                    [
                        (
                            item.card,
                            routed[item.card.vacancy_id].score,
                            routed[item.card.vacancy_id].breakdown,
                        )
                        for item in merged
                        if routed.get(item.card.vacancy_id, None)
                        and routed[item.card.vacancy_id].resume is resume
                    ],
                    key=lambda item: -item[1],
                )
                for resume in resumes
            }
        else:
            cards_by_resume = None
            ranked_by_resume = None
        try:
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
                        True,
                        ranked_by_resume[resume.id],
                    )
                failed = result or failed
        except ApplyRunStopped:
            failed = True
    return failed

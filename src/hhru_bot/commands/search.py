"""Команда search: поиск вакансий по фильтрам резюме (без откликов)."""

from __future__ import annotations

import argparse

from ._common import add_common_args, resumes_from_args


def register(subparsers) -> None:
    p = subparsers.add_parser("search", help="Найти вакансии по фильтрам резюме (без откликов)")
    add_common_args(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..search import filter_candidates, search_vacancies

    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = resumes_from_args(config, args)

    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        for resume in resumes:
            print(f"\n=== Поиск вакансий для резюме: {resume.id} ===")
            cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
            candidates, skipped = filter_candidates(cards, resume.search, resume.id, history)

            print(
                f"Найдено всего: {len(cards)}, "
                f"подходящих: {len(candidates)}, исключено: {len(skipped)}"
            )
            for c in candidates:
                print(f"  [candidate] {c.title} — {c.company} ({c.url})")
            for card, reason in skipped:
                print(f"  [skip] {card.title} — {reason}")

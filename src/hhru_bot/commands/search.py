"""Команда search: поиск вакансий по фильтрам резюме (без откликов)."""

from __future__ import annotations

import argparse

from ..search import SalaryInfo, VacancyCard
from ._common import add_common_args, resumes_from_args


def register(subparsers) -> None:
    p = subparsers.add_parser("search", help="Найти вакансии по фильтрам резюме (без откликов)")
    add_common_args(p)
    p.set_defaults(func=run)


def _format_salary(salary: SalaryInfo | None) -> str:
    """Человекочитаемая зарплата для вывода, пустая строка если её нет."""
    if salary is None:
        return ""
    if salary.salary_from is not None and salary.salary_to is not None:
        # Совпадающие границы — фиксированное значение, без тире.
        if salary.salary_from == salary.salary_to:
            amount = f"{salary.salary_from}"
        else:
            amount = f"{salary.salary_from}-{salary.salary_to}"
    elif salary.salary_from is not None:
        amount = f"от {salary.salary_from}"
    elif salary.salary_to is not None:
        amount = f"до {salary.salary_to}"
    else:
        return ""
    return f"{amount} {salary.currency}"


def _format_card_line(card: VacancyCard) -> str:
    """Дополняет базовую строку карточки зарплатой и датой, если они есть.

    Оба поля опциональны — вакансия без зарплаты/даты выводится как раньше,
    без «з/п не указана» и пустых скобок.
    """
    extras: list[str] = []
    salary = _format_salary(card.salary)
    if salary:
        extras.append(salary)
    if card.raw_date:
        extras.append(card.raw_date)
    suffix = f" | {' / '.join(extras)}" if extras else ""
    return f"{card.title} — {card.company} ({card.url}){suffix}"


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..search import filter_candidates, rank_candidates, search_vacancies

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
            candidates, skipped = filter_candidates(cards, resume.search, resume.resume_id, history)
            ranked = rank_candidates(candidates, resume.search, resume)

            print(
                f"Найдено всего: {len(cards)}, "
                f"подходящих: {len(candidates)}, исключено: {len(skipped)}"
            )
            for c, score, breakdown in ranked:
                factors = ", ".join(
                    f"{name}={value:+.2f}" for name, value in breakdown.items() if value
                )
                detail = f" | {factors}" if factors else ""
                print(f"  [candidate] score={score:+.2f} {_format_card_line(c)}{detail}")
            for card, reason in skipped:
                print(f"  [skip] {_format_card_line(card)} — {reason}")

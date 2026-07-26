"""Общий код команд CLI: разбор резюме, общие аргументы, контекст запуска.

Владелец будущих правок здесь — #2 (stats и т.п. общие расширения). Команды
(login/search/apply/bump/run) живут каждое в своём модуле и авторегистрируются
через register(subparsers) — см. cli.build_parser.
"""

from __future__ import annotations

import argparse
import logging

from ..apply import apply_to_vacancy
from ..config import AppConfig, ResumeConfig
from ..history import History
from ..search import filter_candidates, search_vacancies
from ..throttle import LimitReached, Throttle

logger = logging.getLogger("hhru_bot.cli")


def add_common_args(p: argparse.ArgumentParser) -> None:
    """Общие аргументы для команд, работающих по резюме/поиску."""
    p.add_argument("--resume", help="ID резюме из конфига (по умолчанию — все)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, без реальных действий",
    )
    p.add_argument("--max-pages", type=int, default=5, help="Максимум страниц поиска")


def resolve_resumes(config: AppConfig, resume_ids: list[str] | None) -> list[ResumeConfig]:
    if not resume_ids:
        return config.resumes
    return [config.get_resume(rid) for rid in resume_ids]


def resumes_from_args(config: AppConfig, args: argparse.Namespace) -> list[ResumeConfig]:
    return resolve_resumes(config, [args.resume] if args.resume else None)


def run_apply_for_resume(
    page,
    config: AppConfig,
    resume: ResumeConfig,
    history: History,
    throttle: Throttle,
    args: argparse.Namespace,
) -> None:
    """Цикл откликов по одному резюме (search → filter → apply с троттлингом).

    Перенесено дословно из cli._apply_for_resume. Принципы CLAUDE.md сохранены:
    дедупликация и стоп-листы через filter_candidates (history-based),
    дневной лимит проверяется перед каждым откликом, throttle.wait между откликами.
    """
    print(f"\n=== Отклики для резюме: {resume.id} ===")

    try:
        throttle.check_apply_limit(resume.id, args.dry_run)
    except LimitReached as e:
        print(f"Пропуск: {e}")
        return

    cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
    candidates, skipped = filter_candidates(cards, resume.search, resume.id, history)

    for card, reason in skipped:
        logger.debug("Пропуск вакансии %s: %s", card.title, reason)

    limit = args.limit if args.limit else len(candidates)
    cover_letter_template = config.cover_letter_for(resume)

    applied_count = 0
    for card in candidates[:limit]:
        try:
            throttle.check_apply_limit(resume.id, args.dry_run)
        except LimitReached as e:
            print(f"Дневной лимит достигнут, останавливаюсь: {e}")
            break

        result = apply_to_vacancy(page, card, resume.id, cover_letter_template, args.dry_run)
        status = "dry_run" if args.dry_run else ("success" if result.success else "failed")
        history.record_action(resume.id, card.vacancy_id, "apply", status, result.reason)

        if result.success:
            applied_count += 1
            print(f"  [OK] {card.title} — {card.company}")
        else:
            print(f"  [FAIL] {card.title} — {result.reason}")

        throttle.wait(f"после отклика на '{card.title}'")

    print(f"Итого откликов за этот запуск: {applied_count}")

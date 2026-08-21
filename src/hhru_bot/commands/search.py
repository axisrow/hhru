"""Команда search: поиск вакансий по фильтрам резюме (без откликов)."""

from __future__ import annotations

import argparse
import logging

from ..history import History
from ..search import SalaryInfo, VacancyCard
from ._common import add_common_args, resumes_from_args

logger = logging.getLogger("hhru_bot.cli")


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
    """Дополняет базовую строку карточки зарплатой, если она есть.

    Поле опционально — вакансия без зарплаты выводится как раньше, без
    «з/п не указана» и пустых скобок.
    """
    extras: list[str] = []
    salary = _format_salary(card.salary)
    if salary:
        extras.append(salary)
    suffix = f" | {' / '.join(extras)}" if extras else ""
    return f"{card.title} — {card.company} ({card.url}){suffix}"


def _record_seen(cards: list[VacancyCard], search_query: str, history: History) -> None:
    """Записывает собранные карточки в vacancies_seen (#66, рынок).

    Побочный эффект сбора — НЕ влияет на поиск/скоринг/вывод. Пишет ВСЕ
    собранные карточки (до фильтра stop-списками/историей): рынок хочет полную
    картину сферы, а не только тех, на кого решился откликнуться. Зарплата из
    SalaryInfo (#34); None → «з/п не указана» (тоже пишется для доли рынка без
    зарплаты). Сбой записи НЕ должен валить поиск — рынок лишь удобство.

    employer_tier (#93): classify_employer(company, employer_info) на каждую
    карточку — уровень известности (top_tech/big_corp/mid/unknown). Нужен для
    estimate_salary (медиана salary_to по (search_query, tier) — оценка ЗП для
    вакансий без указанной). Локальный импорт classify_employer: scoring лениво
    тянет search (цикл), тащить его на уровень модуля search-команды не нужно.
    """
    from ..scoring import classify_employer

    for card in cards:
        salary = card.salary
        tier = classify_employer(card.company, getattr(card, "employer_info", None))
        try:
            history.upsert_vacancy_seen(
                vacancy_id=card.vacancy_id,
                search_query=search_query,
                title=card.title,
                company=card.company,
                salary_from=salary.salary_from if salary else None,
                salary_to=salary.salary_to if salary else None,
                salary_currency=salary.currency if salary else None,
                employer_tier=tier,
                vacancy_text=card.vacancy_text,
                published_at=card.published_at.isoformat() if card.published_at else None,
            )
        except Exception as e:  # noqa: BLE001 — рынок не должен валить поиск
            logger.warning("Не записать вакансию %s в рынок: %s", card.vacancy_id, e)


def run(args: argparse.Namespace) -> bool:
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..search import (
        VacancySearchIndeterminate,
        filter_candidates,
        rank_candidates,
        search_vacancies,
    )

    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = resumes_from_args(config, args)

    failed = False
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        for resume in resumes:
            print(f"\n=== Поиск вакансий для резюме: {resume.id} ===")
            try:
                cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
            except VacancySearchIndeterminate as e:
                # Не выдаём непроверенную выдачу за «вакансий нет» и не прячем
                # диагностический отказ за traceback. Следующее резюме можно
                # обработать независимо.
                #
                # cycle-review PR #460 (round 1): partial_results — недостоверный
                # снимок (сам процесс поиска не подтверждён), не «частичный успех».
                # Раньше их без continue пропускали дальше в _record_seen (рынок
                # получал недостоверные данные) и filter_candidates/rank_candidates
                # (печатались как подтверждённые кандидаты) — fail-closed: просто
                # переходим к следующему резюме, ничего не записываем и не выводим.
                print(
                    f"[FAIL] {e}; state={e.state} page={e.page_num} url={e.url} "
                    f"partial_results={len(e.partial_results)} diagnostics={e.diagnostics}"
                )
                failed = True
                continue
            # #66: запись собранных карточек в рынок (побочный эффект сбора) —
            # между search_vacancies и filter_candidates, не трогая отбор/скоринг.
            _record_seen(cards, resume.search.text, history)
            # pre-LLM фильтр работодателя (#85): пороги из опц. scoring.prefilter.
            prefilter = getattr(getattr(resume, "scoring", None), "prefilter", None)
            candidates, skipped = filter_candidates(
                cards, resume.search, resume.resume_id, history, prefilter
            )
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
    return failed

"""Команда market: сравнение сфер по медианной зарплате (#66, #119).

Браузер НЕ нужен — только общая рыночная база data/market.db (таблица
vacancies_seen, которую наполняет search как побочный эффект сбора, #1106;
в history.db она с #1109 больше не создаётся). READ-only: SELECT через
MarketStore.market_salary_by_query, ничего не пишет.

Данные собирает `search`; эта команда только показывает уже собранное. Пустой
рынок → подсказка запустить search (текст даёт market_summary).
НИКАКИХ эмодзи (правило проекта: CLI-вывод чистый текст/ASCII).
"""

from __future__ import annotations

import argparse


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "market",
        help="Сравнение сфер по медианной зарплате (по данным search)",
    )
    p.add_argument(
        "--estimates",
        action="store_true",
        help=(
            "Достроить медиану эвристическими оценками ЗП для вакансий без "
            "указанной (помечаются ~). По умолчанию только реальные ЗП"
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..market_store import MarketStore
    from ..report_market import market_summary

    # Рынок общий на все аккаунты (#1106): vacancies_seen живёт в data/market.db
    # и читается всегда, независимо от --account/args.history.
    history = MarketStore()
    rows = history.market_salary_by_query(include_estimates=args.estimates)
    print(market_summary(rows))
    ages = history.vacancy_age_distribution()
    print("\nВозраст вакансий:")
    for bucket, count in ages.items():
        print(f"{bucket}: {count}")

"""Команда skipped (#392): read-view журнала отсева вакансий.

Браузер не нужен: команда читает локальную SQLite-историю и связывает
записи skipped с уже сохранёнными карточками vacancies_seen.
"""

from __future__ import annotations

import argparse

from ..history import SKIP_REASON_VALUES, History
from ..report_skipped import format_skipped


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "skipped",
        help="Показать журнал пропущенных вакансий",
        description="Показать записи skipped с данными вакансий из локальной истории.",
    )
    p.add_argument(
        "--reason",
        choices=SKIP_REASON_VALUES,
        default=None,
        help="Показать только эту причину (по умолчанию — все причины)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    """Печатает записи skipped без изменения локальной истории."""
    history = History(args.history)
    print(format_skipped(history.list_skipped(args.reason)))

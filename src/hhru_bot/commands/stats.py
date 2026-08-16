"""Команда stats: агрегаты и экспорт истории откликов/поднятий (#11).

Браузер НЕ нужен — только SQLite-история. Вывод идёт в stdout (экспорт):
ASCII-таблицы/текстовые префиксы для table, CSV для csv, Markdown для md.
НИКАКИХ эмодзи (правило проекта: CLI-вывод чистый текст/ASCII).
"""

from __future__ import annotations

import argparse

PERIODS = ("today", "week", "month", "all")
FORMATS = ("table", "csv", "md")


def register(subparsers) -> None:
    p = subparsers.add_parser("stats", help="Сводка и экспорт истории откликов/поднятий")
    p.add_argument("--resume", help="ID резюме (по умолчанию — все)")
    p.add_argument(
        "--period",
        choices=PERIODS,
        default="all",
        help="Период агрегации (по умолчанию all)",
    )
    p.add_argument(
        "--format",
        choices=FORMATS,
        default="table",
        help="Формат вывода: table (по умолчанию), csv, md",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="Вместо сводки вывести список последних действий",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Лимит строк в режиме --list (по умолчанию 50)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    import sys

    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..report import format_actions, format_replies, format_summary

    # --resume получает slug из конфига (resume.id), но история apply/bump
    # хранится под resume.resume_id (стабильный числовой id hh.ru — см. #2).
    # Резолвим slug -> resume.resume_id тем же ключом, что и записи в БД.
    config = load_config_or_exit(args.config)
    resume_id = None
    if args.resume is not None:
        try:
            resume = config.get_resume(args.resume)
        except ConfigError as e:
            print(f"Ошибка конфигурации: {e}", file=sys.stderr)
            sys.exit(1)
        resume_id = resume.resume_id

    history = History(args.history)

    if args.list:
        rows = history.list_actions(resume_id=resume_id, period=args.period, limit=args.limit)
        print(format_actions(rows, args.format))
    else:
        summary = history.summary(resume_id=resume_id, period=args.period)
        print(format_summary(summary, args.format))
        print(
            format_replies(
                history.reply_summary(resume_id=resume_id, period=args.period), args.format
            )
        )

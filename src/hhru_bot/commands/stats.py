"""Команда stats: агрегаты и экспорт истории откликов/поднятий (#11).

Браузер НЕ нужен — только SQLite-история. Вывод идёт в stdout (экспорт):
ASCII-таблицы/текстовые префиксы для table, CSV для csv, Markdown для md.
НИКАКИХ эмодзи (правило проекта: CLI-вывод чистый текст/ASCII).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

PERIODS = ("today", "week", "month", "all")
FORMATS = ("table", "csv", "md")


def register(subparsers) -> None:
    p = subparsers.add_parser("stats", help="Сводка и экспорт истории откликов/поднятий")
    p.add_argument("--resume", help="Slug из конфига или resume_id HH.ru (по умолчанию — все)")
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
        from ._common import resolve_resume

        try:
            resume = resolve_resume(config, args.resume)
        except ConfigError as e:
            print(f"Ошибка конфигурации: {e}", file=sys.stderr)
            sys.exit(1)
        resume_id = resume.resume_id

    history = History(args.history)

    # Назначения внешних тестов — read-only факты без автоматического перехода
    # на сторонний домен. Пока отдельного acknowledgement нет, все записи
    # считаются ожидающими внимания пользователя.
    test_since = datetime.min
    if args.period == "today":
        test_since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    elif args.period in {"week", "month"}:
        test_since = datetime.now() - timedelta(days={"week": 7, "month": 30}[args.period])
    pending_tests = len(history.test_assignments_since(test_since))

    if args.list:
        rows = history.list_actions(resume_id=resume_id, period=args.period, limit=args.limit)
        print(format_actions(rows, args.format))
    else:
        summary = history.summary(resume_id=resume_id, period=args.period)
        print(format_summary(summary, args.format))
        if args.format != "csv":
            print(f"Тестов ожидает: {pending_tests}")
            questionnaire_answers = history.questionnaire_answer_summary()
            print(
                "Анкеты: профиль={profile}, LLM={llm}, не закрыто={unanswered}".format(
                    **questionnaire_answers
                )
            )
        # CSV — экспорт для машин: один документ, одна схема колонок (см. #112
        # ревью). Reply-сводка имеет другую схему (metric,value), поэтому в csv
        # её печатать вторым документом в тот же stdout-поток нельзя — консьюмер,
        # парсящий вывод одним csv.reader, получит смешение схем. Показываем её
        # только в человекочитаемых форматах.
        if args.format != "csv":
            print(
                format_replies(
                    history.reply_summary(resume_id=resume_id, period=args.period), args.format
                )
            )

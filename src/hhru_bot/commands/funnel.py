"""Команда funnel (#13): воронка поиска работы по резюме.

Воронка отправлено → просмотрено → приглашение → оффер с конверсиями между
шагами, плюс опциональная «мёртвая зона» (отклики без ответа старше N дней).

Браузер НЕ нужен — только SQLite-история + JOIN actions × responses.
Вывод идёт в stdout: ASCII-таблицы (table) или markdown (md). НИКАКИХ эмодзи
(правило проекта: CLI-вывод чистый текст/ASCII).

Отдельная команда (а не флаг stats --funnel), чтобы не трогать stats.py —
его может править параллельный воркер; регистрируется автоматически через
pkgutil.iter_modules в cli.register_commands (cli.py не трогается).
"""

from __future__ import annotations

import argparse
import sys

PERIODS_DEFAULT_DAYS = 30
FORMATS = ("table", "md")


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "funnel",
        help="Воронка откликов: отправлено → просмотрено → приглашение → оффер",
    )
    p.add_argument("--resume", help="Slug из конфига или resume_id HH.ru (по умолчанию — все)")
    p.add_argument(
        "--search-query",
        action="store_true",
        help="Группировать воронку по поисковому запросу вместо резюме",
    )
    p.add_argument(
        "--format",
        choices=FORMATS,
        default="table",
        help="Формат вывода: table (по умолчанию) или md",
    )
    p.add_argument(
        "--period",
        type=int,
        default=PERIODS_DEFAULT_DAYS,
        help="Срез за последние N дней (по умолчанию 30; 0 = за всё время)",
    )
    p.add_argument(
        "--dead",
        action="store_true",
        help="Показать «мёртвую зону»: отклики без ответа старше --dead-days",
    )
    p.add_argument(
        "--dead-days",
        type=int,
        default=14,
        help="Порог «мёртвой зоны» в днях (по умолчанию 14)",
    )
    p.add_argument(
        "--rejections",
        action="store_true",
        help="Показать агрегат отказов по работодателю, поиску и вилке зарплаты",
    )
    p.set_defaults(func=run)


def _resolve_resume_id(args: argparse.Namespace):
    """Резолвит --resume (slug или resume_id HH.ru, #319) → resume.resume_id. None = все."""
    from ..config import ConfigError, load_config_or_exit
    from ._common import resolve_resume

    if args.resume is None:
        return None
    config = load_config_or_exit(args.config)
    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as e:
        print(f"Резюме не найдено: {e}", file=sys.stderr)
        sys.exit(1)
    return resume.resume_id


def run(args: argparse.Namespace) -> None:
    from datetime import datetime, timedelta

    from ..history import History
    from ..report_funnel import format_dead, format_funnel, format_rejections

    resume_id = _resolve_resume_id(args)

    # since: отсечка created_at за N дней, либо None (period=0 → за всё время).
    if args.period and args.period > 0:
        since = (datetime.now() - timedelta(days=args.period)).isoformat()
    else:
        since = None

    history = History(args.history)

    if getattr(args, "rejections", False):
        rejections = history.rejections_by_employer(since=since, resume_id=resume_id)
        print(format_rejections(rejections, args.format))
        return

    if args.dead:
        # «мёртвая зона»: доля откликов без ответа старше --dead-days.
        dead = history.dead_responses(days=args.dead_days, resume_id=resume_id)
        print(format_dead(dead, args.format))
        return

    # На пустой истории воронка печатает шапку таблицы (формат стабилен, как
    # format_actions в report.py) — пользователь видит структуру даже без данных.
    if args.search_query:
        funnel = history.funnel_by_search_query(since=since, resume_id=resume_id)
        print(format_funnel(funnel, args.format, group_key="search_query"))
        # #411 code review: INNER JOIN к vacancies_seen молча теряет отклики,
        # сделанные через apply/run без предварительного отдельного search по
        # тем же вакансиям (vacancies_seen заполняет только команда search).
        # Числа воронки при этом корректны для того, что она видит — просто
        # неполны; предупреждаем, а не оставляем расхождение незаметным.
        unattributed = history.count_unattributed_applies(since=since, resume_id=resume_id)
        if unattributed:
            print(
                f"[INFO] {unattributed} отклик(ов) не привязаны к поисковому "
                "запросу и не вошли в воронку выше — вакансии не были собраны "
                "командой search"
            )
    else:
        funnel = history.funnel_by_resume(since=since, resume_id=resume_id)
        print(format_funnel(funnel, args.format))

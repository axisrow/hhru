"""Команда learn: что подтянуть по собранным вакансиям (#391)."""

from __future__ import annotations

import argparse


def register(subparsers) -> None:
    p = subparsers.add_parser("learn", help="Что подтянуть: навыки из собранных вакансий")
    p.add_argument("--resume", help="Slug резюме для исключения уже указанных навыков")
    p.add_argument("--limit", type=int, default=20, help="Сколько строк вывести")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..report import _ascii_table
    from ..skill_gaps import aggregate_skill_gaps

    current: list[str] = []
    if args.resume:
        config = load_config_or_exit(args.config)
        from ._common import resolve_resume

        try:
            resume = resolve_resume(config, args.resume)
        except ConfigError as exc:
            raise SystemExit(f"Ошибка конфигурации: {exc}") from exc
        current = list(getattr(getattr(resume, "ai_profile", None), "skills", []))
    rows = aggregate_skill_gaps(
        History(args.history).list_vacancy_texts(), current, max(0, args.limit)
    )
    print(
        _ascii_table(["skill", "vacancies"], [[str(r["skill"]), str(r["vacancies"])] for r in rows])
    )

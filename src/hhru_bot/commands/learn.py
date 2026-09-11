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
    from ..market_store import open_market
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
    # #1109: собранные тексты вакансий живут в общей market.db (в history.db
    # таблицы vacancies_seen больше нет); недоступна — пустой вход, таблица
    # «навыков не собрано».
    market = open_market()
    texts = market.list_vacancy_texts() if market is not None else []
    rows = aggregate_skill_gaps(texts, current, max(0, args.limit))
    print(
        _ascii_table(["skill", "vacancies"], [[str(r["skill"]), str(r["vacancies"])] for r in rows])
    )

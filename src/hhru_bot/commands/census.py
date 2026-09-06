"""census: показать, что РЕАЛЬНО отрисовано на странице (#1002).

Агенты «слепы»: сырой HTML-дамп содержит JSON-состояние и i18n-словари,
текстовые литералы которых неотличимы от UI при поиске по подстроке
(ложные «8 Городов» #998). Команда открывает URL и печатает census —
таблицу отрисованных DOM-контролов (data-qa/tag/role/label/text/visible).
Read-only: никаких кликов и отправок формы.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..browser import (
    census_table,
    goto_hh,
    launch_context,
    rendered_controls_census,
)


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "census",
        help="Таблица отрисованных контролов страницы (агентские «глаза», read-only)",
    )
    parser.add_argument("--url", required=True, help="Полный URL страницы hh.ru")
    parser.add_argument("--json", action="store_true", help="Машиночитаемый JSON-вывод")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..config import load_config

    config = load_config(args.config)
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        goto_hh(page, args.url)
        rows = rendered_controls_census(page)

    visible_only = [r for r in rows if r.get("visible")]
    if getattr(args, "json", False):
        import json

        print("MACHINE_READABLE_JSON:")
        print(json.dumps({"url": args.url, "controls": rows}, ensure_ascii=False, indent=2))
        return False

    print(f"URL: {args.url}")
    print(f"Контролов всего: {len(rows)}, видимых: {len(visible_only)}")
    print(census_table(visible_only))
    print("[OK] census read-only; вхождения строк в HTML-дампе (JSON/i18n) — не поля")
    return False

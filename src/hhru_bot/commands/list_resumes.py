"""Команда list-resumes (#57): список резюме из конфига (READ, #21).

Top-level команда ``hhru_bot list-resumes [--status]`` — регистрируется
автоматически через pkgutil.iter_modules (cli.py не трогается).

READ-команда: читает ``config.resumes`` локально из config.yaml и печатает
ASCII-таблицу (``report._ascii_table``). hh.ru НЕ дёргается.

Флаг ``--status`` дополнительно показывает статус поднятия резюме, читая ЛОКАЛЬНУЮ
историю (без обращения к аккаунту):
  - «можно bump» — ``throttle.can_bump_now()`` (кулдаун 4ч сверх дневного лимита);
  - «последний bump» — ``history.last_action_at(resume_id, 'bump')`` (дата
    последнего успешного поднятия из actions, или «—» если не поднимали).

Контракт вывода — docs/cli-spec.md §list-resumes: колонки
``id | resume_id | можно bump | последний bump`` (последние две — только с
``--status``). ``id`` — slug из конфига; ``resume_id`` — числовой хвост
``resume_url`` (``ResumeConfig.resume_id``). Только текст/ASCII, без эмодзи.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from ..report import _ascii_table


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "list-resumes",
        help="Список резюме из конфига (READ; --status — статус поднятия из истории)",
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Дополнительно: можно ли поднять (кулдаун) и дата последнего поднятия",
    )
    p.set_defaults(func=run)


def _format_bump_cell(can_bump: bool, wait_left) -> str:
    """Колонка «можно bump»: «да» / «нет (Xч Yм)».
    wait_left — timedelta | None: None при can_bump=True (кулдаун прошёл/не было bump).
    """
    if can_bump:
        return "да"
    # can_bump=False → wait_left гарантированно не None (см. throttle.can_bump_now).
    total_seconds = int(wait_left.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"нет ({hours}ч {minutes}м)"
    return f"нет ({minutes}м)"


def _format_last_bump_cell(last_at: datetime | None) -> str:
    """Колонка «последний bump»: «YYYY-MM-DD HH:MM» или «—» (нет успешных bump)."""
    if last_at is None:
        return "—"
    return last_at.strftime("%Y-%m-%d %H:%M")


def run(args: argparse.Namespace) -> None:
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import Throttle

    config = load_config_or_exit(args.config)
    history = History(args.history)
    throttle = Throttle(config.throttle, history)

    if args.status:
        header = ["id", "resume_id", "можно bump", "последний bump"]
    else:
        header = ["id", "resume_id"]

    rows: list[list[str]] = []
    for resume in config.resumes:
        row = [resume.id, resume.resume_id]
        if args.status:
            can_bump, wait_left = throttle.can_bump_now(resume.resume_id)
            last_at = history.last_action_at(resume.resume_id, "bump")
            row.append(_format_bump_cell(can_bump, wait_left))
            row.append(_format_last_bump_cell(last_at))
        rows.append(row)

    print(_ascii_table(header, rows))

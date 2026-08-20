"""Manual vacancy rejection feedback (#417).

This is intentionally independent of the review queue from #414.  It records
the user's reason and, when supplied, a bounded/redacted diff of a generated
letter and its edited version.
"""

from __future__ import annotations

import argparse
import sys


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "reject", help="Записать ручное отклонение вакансии и feedback по письму"
    )
    parser.add_argument("--resume", required=True, help="ID резюме")
    parser.add_argument("--vacancy", required=True, help="ID вакансии")
    parser.add_argument("--reason", required=True, help="Причина ручного отклонения")
    parser.add_argument("--generated-letter", help="Сгенерированное письмо до ручной правки")
    parser.add_argument("--edited-letter", help="Письмо после ручной правки")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..history import History

    try:
        action_id = History(args.history).record_reject(
            args.resume,
            args.vacancy,
            args.reason,
            generated_letter=args.generated_letter,
            edited_letter=args.edited_letter,
        )
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return True
    print(f"[OK] Вакансия {args.vacancy} отклонена (feedback #{action_id}).")
    return False

"""Read-only listing of employer robot questionnaires."""

from __future__ import annotations


def register(subparsers) -> None:
    parser = subparsers.add_parser("robot-queue", help="Показать диалоги с анкетами-роботами")
    parser.add_argument("--limit", type=int, default=50)
    parser.set_defaults(func=run)


def run(args) -> None:
    from ..history import History

    if args.limit < 1:
        print("[FAIL] --limit должен быть >= 1")
        return
    rows = History(args.history).list_robot_questionnaires(args.limit)
    if not rows:
        print("[INFO] Очередь анкет-роботов пуста.")
        return
    for row in rows:
        print(f"{row['topic']} — вакансия {row['vacancy_id'] or '?'} — {row['reason']}")

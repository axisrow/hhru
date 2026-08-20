"""Управление локальной очередью согласования откликов (#414)."""

from __future__ import annotations

import argparse


def register(subparsers) -> None:
    p = subparsers.add_parser("review", help="Очередь вакансий на согласование")
    actions = p.add_subparsers(dest="review_action", required=True)
    ls = actions.add_parser("list", help="Показать очередь")
    ls.add_argument(
        "--status", choices=["pending", "approved", "applying", "applied", "failed", "skipped"]
    )
    edit = actions.add_parser("edit", help="Изменить письмо")
    edit.add_argument("id", type=int)
    edit.add_argument("letter")
    approve = actions.add_parser("approve", help="Одобрить запись и выдать одноразовый permit")
    approve.add_argument("id", type=int)
    approve.add_argument("--ttl", type=int, default=900)
    skip = actions.add_parser("skip", help="Пропустить запись")
    skip.add_argument("id", type=int)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..history import History

    history = History(args.history)
    if args.review_action == "list":
        for row in history.review_items(args.status):
            print(
                f"{row['id']} [{row['status']}] {row['resume_id']} "
                f"{row['title']} — {row['company']}"
            )
        return False
    if args.review_action == "edit":
        history.edit_review_letter(args.id, args.letter)
        return False
    if args.review_action == "approve":
        print(history.approve_review(args.id, args.ttl))
        return False
    with history._connect() as conn:
        cur = conn.execute(
            "UPDATE review_queue SET status='skipped', updated_at=datetime('now') "
            "WHERE id=? AND status='pending'",
            (args.id,),
        )
        if cur.rowcount != 1:
            raise ValueError("запись очереди не найдена или уже обработана")
    return False

"""Operator queue for actions whose remote outcome is unknown."""

from __future__ import annotations

import argparse


def register(subparsers) -> None:
    p = subparsers.add_parser("uncertain", help="Очередь неподтверждённых действий")
    sub = p.add_subparsers(dest="uncertain_command", required=True)
    listing = sub.add_parser("list", help="Показать unresolved записи")
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=list_run)
    inspect = sub.add_parser("inspect", help="Открыть evidence и план readback")
    inspect.add_argument("id", type=int)
    inspect.set_defaults(func=inspect_run)
    reconcile = sub.add_parser("reconcile", help="Выполнить только READ-проверку")
    reconcile.add_argument("id", type=int)
    reconcile.set_defaults(func=reconcile_run)


def _history(args):
    from ..history import History

    return History(args.history)


def list_run(args: argparse.Namespace) -> None:
    rows = _history(args).list_unresolved_uncertain(args.limit)
    if not rows:
        print("[INFO] Очередь uncertain пуста.")
        return
    for row in rows:
        command = row.get("command") or row["action"]
        target = f"resume={row['resume_id']} vacancy={row['vacancy_id']}"
        print(
            f"#{row['id']} {command} {target} time={row['created_at']} "
            f"reason={row.get('reason') or '-'}"
        )


def inspect_run(args: argparse.Namespace) -> None:
    row = _history(args).get_uncertain(args.id)
    if row is None:
        print(f"[FAIL] unresolved uncertain #{args.id} не найдена")
        return
    print(f"id: {row['id']}")
    print(f"command: {row.get('command') or row['action']}")
    print(f"resume: {row['resume_id']}")
    print(f"vacancy: {row['vacancy_id']}")
    print(f"evidence: {row.get('reason') or '-'}")
    print("readback: command-specific authoritative READ; WRITE повторно не выполняется")


def reconcile_run(args: argparse.Namespace) -> None:
    row = _history(args).get_uncertain(args.id)
    if row is None:
        print(f"[FAIL] unresolved uncertain #{args.id} не найдена")
        return
    print("[FAIL] verifier для этой команды ещё не поддержан; retry barrier сохранён")

"""Manage persistent blacklist rules (local SQLite only)."""

import argparse


def register(subparsers) -> None:
    p = subparsers.add_parser("blacklist", help="Управление стоп-листом вакансий")
    s = p.add_subparsers(dest="blacklist_command", required=True)
    a = s.add_parser("add")
    a.add_argument("type", choices=("company", "keyword", "vacancy"))
    a.add_argument("value")
    a.add_argument("--reason", required=True)
    a.add_argument("--by", default="cli")
    list_parser = s.add_parser("list")
    r = s.add_parser("remove")
    r.add_argument("type", choices=("company", "keyword", "vacancy"))
    r.add_argument("value")
    for x in (a, list_parser, r):
        x.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..history import History

    h = History(args.history)
    if args.blacklist_command == "add":
        h.add_blacklist(args.type, args.value, args.reason, args.by)
        print("[OK] правило добавлено")
    elif args.blacklist_command == "remove":
        print(f"[OK] удалено: {h.remove_blacklist(args.type, args.value)}")
    else:
        for row in h.list_blacklist():
            print(f"{row['entry_type']}\t{row['value']}\t{row['created_by']}\t{row['reason']}")
    return False

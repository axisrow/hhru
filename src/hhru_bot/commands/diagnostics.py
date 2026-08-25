from __future__ import annotations

import argparse
from pathlib import Path

from ..diagnostics import _same_path, export_bundle


def register(subparsers) -> None:
    p = subparsers.add_parser("diagnostics", help="Офлайн диагностика сохранённого command run")
    sub = p.add_subparsers(dest="diagnostics_command", required=True)
    e = sub.add_parser("export", help="Экспорт воспроизводимого incident bundle")
    e.add_argument("--run-id")
    e.add_argument("--output", type=Path)
    e.add_argument("--log", type=Path, default=Path("data/logs/hhru_bot.log"))
    e.add_argument("--dom-dir", type=Path, default=Path("data/logs"))
    e.set_defaults(func=run)


def run(args: argparse.Namespace):
    text = export_bundle(
        history=Path(args.history), run_id=args.run_id, log_path=args.log, dom_dir=args.dom_dir
    )
    if args.output:
        output = args.output.expanduser().resolve()
        history = Path(args.history).expanduser().resolve()
        if any(
            _same_path(output, candidate)
            for candidate in (
                history,
                history.with_name(history.name + "-wal"),
                history.with_name(history.name + "-shm"),
            )
        ):
            raise ValueError("incident bundle нельзя записать поверх history.db")
        if _same_path(output, args.log):
            raise ValueError("incident bundle нельзя записать поверх исходного лога")
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")

from __future__ import annotations

import argparse
from pathlib import Path

from ..diagnostics import export_bundle


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
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")

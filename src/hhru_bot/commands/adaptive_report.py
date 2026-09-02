"""Read-only adaptive-pool quality report (#947)."""

from __future__ import annotations

import argparse


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "adaptive-report", help="Метрика качества пула резюме (только чтение)"
    )
    parser.add_argument("--format", choices=("table",), default="table", help="ASCII-таблица")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..adaptive_metrics import build_adaptive_metrics
    from ..config import load_config_or_exit
    from ..history import History
    from ..report_adaptive import format_adaptive, success_statement

    config = load_config_or_exit(args.config)
    metrics = build_adaptive_metrics(config.resumes, History(args.history).adaptive_report_facts())
    print(format_adaptive(metrics))
    if not metrics or not any(m.samples for m in metrics):
        print("[INFO] insufficient data: score для резюме пока не накоплен")
    print(success_statement(metrics))

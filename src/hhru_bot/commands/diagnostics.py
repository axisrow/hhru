from __future__ import annotations

import argparse
from pathlib import Path

from ..diagnostics import _same_path, export_bundle
from ..provenance import RECOVERY_COMMAND, run_doctor


def register(subparsers) -> None:
    p = subparsers.add_parser("diagnostics", help="Офлайн диагностика сохранённого command run")
    sub = p.add_subparsers(dest="diagnostics_command", required=True)
    e = sub.add_parser("export", help="Экспорт воспроизводимого incident bundle")
    e.add_argument("--run-id")
    e.add_argument("--output", type=Path)
    e.add_argument("--log", type=Path, default=Path("data/logs/hhru_bot.log"))
    e.add_argument("--dom-dir", type=Path, default=Path("data/logs"))
    e.set_defaults(func=run)
    d = sub.add_parser(
        "doctor",
        help="Проверить согласованность CLI, marketplace snapshot и plugin cache",
        description=(
            "Сравнивает версию, release/tag и commit SHA установленного CLI, "
            "marketplace snapshot и загруженного Codex plugin."
        ),
    )
    d.add_argument(
        "--marketplace-path",
        "--marketplace",
        dest="marketplace",
        type=Path,
        help="Путь к marketplace snapshot (для диагностики нестандартной установки)",
    )
    d.add_argument(
        "--plugin-cache",
        type=Path,
        help="Путь к Codex plugin cache (для диагностики нестандартной установки)",
    )
    d.set_defaults(func=run_doctor_command)


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


def run_doctor_command(args: argparse.Namespace) -> bool:
    result = run_doctor(marketplace=args.marketplace, plugin_cache=args.plugin_cache)
    for component in result.components:
        print(f"[{component.name}] {component.describe()}")
    if not result.drift:
        print("[OK] CLI, marketplace snapshot и plugin cache согласованы.")
        return False
    print("[DRIFT] CLI, marketplace snapshot и plugin cache рассинхронизированы.")
    for reason in result.reasons:
        print(f"[DETAIL] {reason}")
    print(f"[FIX] Выполните одну команду: {RECOVERY_COMMAND}")
    return True

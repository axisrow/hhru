"""Unified CLI and Codex plugin update command (#675)."""

from __future__ import annotations

import argparse

from ..update import UpdateError, update


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "update",
        help="Обновить CLI и Codex plugin до одного release/commit",
        description=(
            "Обновляет hhru и установленный Codex plugin из одного commit, "
            "проверяет provenance обоих компонентов и явно завершается ошибкой "
            "при частичном сбое. Уже открытая задача Codex продолжит использовать "
            "старый skill — после обновления начните новую задачу."
        ),
    )
    parser.add_argument(
        "--codex",
        default="codex",
        help="Путь к Codex CLI (для диагностики и тестов; по умолчанию: codex)",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool | None:
    try:
        result = update(codex=args.codex)
    except UpdateError as exc:
        print(f"[FAIL] Обновление hhru не завершено: {exc}")
        print("[INFO] CLI и plugin могли обновиться частично; повторите `hhru update`.")
        return True
    release = result.release
    print(f"[OK] hhru {release.version} обновлён до commit {release.commit}")
    print(f"[OK] CLI provenance: {result.cli_source}")
    print(f"[OK] Codex plugin provenance: {result.plugin_source}")
    print(
        "[INFO] Уже открытая задача Codex продолжит использовать старый skill; "
        "начните новую задачу."
    )
    return None

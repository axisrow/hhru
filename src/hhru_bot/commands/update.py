"""Unified CLI and Codex plugin update command (#675)."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from ..update import UpdateError, update

_WINDOWS_REEXEC_ENV = "HHRU_UPDATE_REEXEC"


def _reexec_windows_launcher() -> bool:
    """Re-exec through Python so ``hhru.exe`` is not locked during pip.

    A Windows console-script launcher remains open for the duration of the
    command and cannot be replaced by pip. Replacing this process image with
    the interpreter releases the launcher before the update starts; the
    environment marker prevents an exec loop. The normal user-facing command
    and its arguments continue unchanged.
    """
    if os.name != "nt" or os.environ.get(_WINDOWS_REEXEC_ENV) == "1":
        return False
    candidates = (
        sys.executable,
        getattr(sys, "_base_executable", ""),
        shutil.which("python") or "",
    )
    interpreter = next(
        (
            candidate
            for candidate in candidates
            if candidate and Path(candidate).name.casefold() not in {"hhru.exe", "hhru-bot.exe"}
        ),
        None,
    )
    if interpreter is None:
        raise UpdateError("не найден Python interpreter для безопасного Windows update")
    os.environ[_WINDOWS_REEXEC_ENV] = "1"
    try:
        # ``os.execve`` is prone to a CPython/UCRT access violation on
        # Windows when called from a subprocess with a copied environment.
        # ``execv`` inherits the already-updated process environment and keeps
        # the launcher handoff without rebuilding that environment block.
        os.execv(interpreter, [interpreter, "-m", "hhru_bot.cli", *sys.argv[1:]])
    except OSError as exc:
        raise UpdateError(f"не удалось перезапустить Windows update через Python: {exc}") from exc
    return True


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

"""Smoke-проверка упакованного дистрибутива: History создаёт таблицы в wheel.

Запускается CI job `packaging` в чистом venv после `pip install dist/*.whl`.
Локально это обычный тест pytest (из checkout). Цель — поймать регрессию, при
которой History перестаёт создавать таблицы при установке пакета (раньше
схема ехала в wheel как .sql-ресурс migrations/*.sql; теперь схема — Python-код
в history.SCHEMA, который всегда входит в пакет, но проверка остаётся
страховкой, что History работает при `pip install .`, а не только из checkout).

Запуск как скрипта: `python tests/packaging_smoke.py` (exit 0 = OK).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # packaging smoke runs without dev dependencies
    pytest = None

pytestmark = pytest.mark.smoke if pytest is not None else ()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plugin-archive",
        type=Path,
        help="also install and inspect the built plugin archive in a clean temporary directory",
    )
    args = parser.parse_args()

    # History создаёт таблицу actions и пишет/читает запись (схема применилась).
    from hhru_bot.history import History

    with tempfile.TemporaryDirectory() as d:
        h = History(os.path.join(d, "h.db"))
        h.record_action("r1", "v1", "apply", "success")
        assert h.has_applied("r1", "v1")

    if args.plugin_archive:
        with tempfile.TemporaryDirectory() as d:
            with tarfile.open(args.plugin_archive, "r:gz") as archive:
                archive.extractall(d, filter="data")
            roots = list(Path(d).iterdir())
            assert len(roots) == 1, roots
            bundle = roots[0]
            manifest = json.loads(
                (bundle / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            assert manifest["name"] == "hhru-cc-plugin"
            skills_path = bundle / manifest["skills"].lstrip("./")
            skill = skills_path / "hhru" / "SKILL.md"
            assert skill.is_file(), skill
            skill_text = skill.read_text(encoding="utf-8")
            assert "--execution-mode foreground" in skill_text
            assert "--progress-verbosity 1" in skill_text

    print("packaging smoke OK")
    sys.exit(0)


if __name__ == "__main__":
    main()

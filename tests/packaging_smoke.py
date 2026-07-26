"""Smoke-проверка упакованного дистрибутива: .sql миграция входит в wheel и History работает.

Запускается CI job `packaging` в чистом venv после `pip install dist/*.whl`.
Локально это обычный тест pytest (из checkout). Цель — поймать регрессию, при которой
package-data перестаёт тащить migrations/*.sql и History не создаёт таблицу actions.

Запуск как скрипта: `python tests/packaging_smoke.py` (exit 0 = OK).
"""

from __future__ import annotations

import os
import sys
import tempfile
from importlib import resources


def main() -> None:
    # 1. .sql миграция доступна как ресурс в установленном пакете.
    files = [p.name for p in resources.files("hhru_bot.migrations").iterdir()]
    assert any(f.endswith(".sql") for f in files), f"нет .sql миграции в пакете: {files}"

    # 2. History создаёт таблицу actions (миграция применилась).
    from hhru_bot.history import History

    with tempfile.TemporaryDirectory() as d:
        h = History(os.path.join(d, "h.db"))
        h.record_action("r1", "v1", "apply", "success")
        assert h.has_applied("r1", "v1")

    print("packaging smoke OK")
    sys.exit(0)


if __name__ == "__main__":
    main()

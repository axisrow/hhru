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

import os
import sys
import tempfile

try:
    import pytest
except ModuleNotFoundError:  # packaging smoke runs without dev dependencies
    pytest = None

pytestmark = pytest.mark.smoke if pytest is not None else ()


def main() -> None:
    # History создаёт таблицу actions и пишет/читает запись (схема применилась).
    from hhru_bot.history import History

    with tempfile.TemporaryDirectory() as d:
        h = History(os.path.join(d, "h.db"))
        h.record_action("r1", "v1", "apply", "success")
        assert h.has_applied("r1", "v1")

    print("packaging smoke OK")
    sys.exit(0)


if __name__ == "__main__":
    main()

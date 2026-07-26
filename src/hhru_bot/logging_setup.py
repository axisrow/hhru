from __future__ import annotations

import logging
from pathlib import Path

# Логи — относительно cwd (точки запуска), не относительно пакета: после
# `pip install` пакет в site-packages, писать логи туда нельзя. См. cli.py.
LOG_DIR = Path.cwd() / "logs"


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "hhru_bot.log"

    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger("hhru_bot")
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

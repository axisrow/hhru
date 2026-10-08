"""overlay-census: снять оверлеи живой вкладки в дамп (#1232).

DX-разведка поверх канала S1 (#1159): команда поднимает тот же loopback-сервер,
что и live-serve/live-doctor, ждёт расширение hhru-live, выполняет read-only
``list_overlays`` и печатает таблицу (id/type/disposition/closeControls/текст),
а JSON-дамп пишет в ``data/logs`` — «глаза» агента до боевого прогона. Никаких
кликов и dismiss'ов: census по определению разведочный (граница с live-doctor:
doctor проверяет здоровье канала, census — содержимое вкладки).
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from ..exit_codes import CommandExitCode
from ..live.scenarios import ChannelError, LiveChannel, PrimitiveError
from ..logging_setup import LOG_DIR
from ..report import _ascii_table
from .live_serve import LIVE_SERVE_DEFAULT_PORT, _port

# Подсказка при недоступном канале — канон live-doctor (_CONNECT_HINT).
_CONNECT_HINT = (
    "запустите Chrome с расширением hhru-live и открытой вкладкой hh.ru — "
    "./scripts/run_extension_chrome.sh"
)


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "overlay-census",
        help=(
            "Снять оверлеи живой вкладки (list_overlays) в таблицу и JSON-дамп "
            "data/logs (READ, разведка без боевого прогона)"
        ),
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=LIVE_SERVE_DEFAULT_PORT,
        help=(
            "Порт сервера на 127.0.0.1 (по умолчанию 8765 — единственный, "
            "к которому расширение подключается само)"
        ),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=10.0,
        help="Сколько секунд ждать подключения расширения",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool | CommandExitCode:
    channel = LiveChannel(port=args.port, client_timeout=args.wait_seconds)
    try:
        url_ws = channel.start()
        print(f"[INFO] overlay-census: канал {url_ws} — жду расширение hhru-live")
        channel.wait_client()
    except ChannelError as exc:
        print(f"[FAIL] overlay-census: {exc}; подсказка: {_CONNECT_HINT}")
        channel.close()
        return True

    try:
        state = channel.get_state()
        overlays = [overlay for overlay in channel.list_overlays() if isinstance(overlay, dict)]
    except (ChannelError, PrimitiveError) as exc:
        print(f"[FAIL] overlay-census: вкладка не ответила на census ({exc})")
        channel.close()
        return True
    finally:
        channel.close()

    page_url = str(state.get("url", ""))
    if page_url:
        print(f"[INFO] вкладка: {page_url}")
    rows = [
        [
            str(o.get("id") or ""),
            str(o.get("type") or ""),
            str(o.get("disposition") or ""),
            str(o.get("closeControls") or 0),
            str(o.get("text") or "").strip()[:60],
        ]
        for o in overlays
    ]
    print(_ascii_table(["id", "type", "disposition", "close", "текст"], rows))

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"overlay_census_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(
            json.dumps(
                {
                    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "url": page_url,
                    "overlays": overlays,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[FAIL] overlay-census: дамп не записан ({exc})")
        return True
    verdict = "оверлеев нет" if not overlays else f"оверлеев: {len(overlays)}"
    print(f"[OK] census read-only, {verdict}; дамп: {path}")
    return False

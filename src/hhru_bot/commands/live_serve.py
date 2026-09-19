"""live-serve: loopback WS-сервер live-канала (#1159, этап 2 эпика #588).

READ (локальный транспорт): сервер ничего не пишет ни на hh.ru, ни на диск и
не запускает браузер — Chrome с расширением hhru-live запускает пользователь.

Сессия живёт только пока команда запущена (foreground): EOF stdin или Ctrl+C
гасят сервер, фоновых демонов нет. Команды подаются по одной JSON-строке
envelope'а ``{"v", "id", "action", "payload"}`` в stdin, ответы приходят
по одной JSON-строке ``{"id", "status", "result"}`` в stdout; события
соединения — префикс ``[INFO]``. Ответ расширения пробрасывается дословно
(transport-level коды ошибок — в protocol.py, коды сценариев — у расширения).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..exit_codes import CommandExitCode
from ..live import ALLOWED_ACTIONS, PROTOCOL_VERSION, LiveServeServer


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "live-serve",
        help=(
            "Loopback WS-сервер live-канала для расширения hhru-live "
            f"(протокол v{PROTOCOL_VERSION}, READ-local, foreground)"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Порт на 127.0.0.1 (0 — свободный ephemeral, печатается при старте)",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool | CommandExitCode:
    server = LiveServeServer(port=args.port)
    try:
        server.bind()
    except OSError as exc:
        print(f"[FAIL] live-serve: не удалось занять 127.0.0.1:{args.port}: {exc}")
        return True
    print(
        f"[INFO] live-serve: слушает {server.url} "
        f"(протокол v{PROTOCOL_VERSION}; allowlist: {', '.join(sorted(ALLOWED_ACTIONS))})"
    )
    print(
        '[INFO] команды — по одной JSON-строке {"v", "id", "action", "payload"} '
        'в stdin; ответы — JSON-строки {"id", "status", "result"} здесь; '
        "Ctrl+C или EOF stdin — остановка"
    )
    # ponytail: select() по stdin-fd не работает на Windows — платформа
    # проекта macOS/Linux; при появлении Windows-прогона нужен поток-читатель.
    server.serve(sys.stdin.fileno(), sys.stdout)
    print(
        f"[OK] live-serve остановлен; подключений: {server.connections_seen}, "
        f"команд переслано: {server.commands_forwarded}"
    )
    return False

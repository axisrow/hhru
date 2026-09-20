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


def _port(value: str) -> int:
    """Валидация диапазона TCP-портов: вне 0..65535 socket.bind бросает
    OverflowError (сырой traceback), поэтому диапазон ловим на argparse,
    как _positive_page_count в commands/_common.py."""
    parsed = int(value)
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError(
            f"порт должен быть в диапазоне 0..65535 (0 — ephemeral), получено {value!r}"
        )
    return parsed


#: Дефолт порта согласован с расширением: background.js подключается к
#: ws://127.0.0.1:8765 жёстко (LIVE_SERVE_URL), другого механизма сказать ему
#: порт у моста нет.
LIVE_SERVE_DEFAULT_PORT = 8765


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
        type=_port,
        default=LIVE_SERVE_DEFAULT_PORT,
        help="Порт на 127.0.0.1 (по умолчанию 8765 — согласован с расширением)",
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

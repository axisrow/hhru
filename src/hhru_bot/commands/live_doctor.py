"""live-doctor: диагностика подключения live-канала (#1163, этап 2 эпика #588).

READ-локально: команда поднимает тот же loopback-сервер, что и live-serve,
ждёт handshake-диагностику (kind-frame ``hello``) от расширения hhru-live и
сверяет её с ожиданиями сервера. Ничего не пишет ни на hh.ru, ни в history;
браузер не запускает и расширение не устанавливает (#1163: только инструкция
при FAIL, никаких инсталляторов).

Проверки, каждая — отдельная строка ``[OK]``/``[FAIL]`` (FAIL несёт подсказку
следующего шага):

1. сервер — bind ``127.0.0.1:<port>`` и приём подключений;
2. расширение — подключилось (Chrome с включённым hhru-live);
3. версия протокола — ``hello.v`` совпадает с серверной ``PROTOCOL_VERSION``;
4. allowlist — действия объявлены одинаково с обеих сторон;
5. permissions — манифест расширения минимален и достаточен.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Any

from ..exit_codes import CommandExitCode
from ..live import ALLOWED_ACTIONS, PROTOCOL_VERSION, LiveServeServer
from .live_serve import _port

# Тот же порт, что LIVE_SERVE_URL в extensions/hhru-live/background.js:
# расширение переподключается только на него (страж — тест в test_live_doctor).
DEFAULT_PORT = 8765
DEFAULT_WAIT_SECONDS = 10.0

# Минимально достаточные permissions манифеста расширения (страж — тест сверяет
# с extensions/hhru-live/manifest.json): storage — session-хранилище отчётов,
# host hh.ru — чтение sender.tab.url и content_scripts.
EXPECTED_PERMISSIONS = frozenset({"storage"})
EXPECTED_HOST_PERMISSIONS = frozenset({"https://hh.ru/*", "https://*.hh.ru/*"})

# Подсказка про подключение расширения — одна для проверок 2-5.
_CONNECT_HINT = (
    "запустите Chrome с расширением hhru-live и открытой вкладкой hh.ru — "
    "./scripts/run_extension_chrome.sh; разовая установка — chrome://extensions, "
    "Developer mode -> Load unpacked -> extensions/hhru-live (автоустановки нет)"
)


def check_version(hello: dict | None) -> tuple[bool, str]:
    """(3) Версия протокола hello против серверной; вердикт + текст строки."""
    if hello is None:
        return False, "нет handshake-данных — расширение не подключилось (см. п.2)"
    received = hello.get("v")
    if not isinstance(received, int) or isinstance(received, bool):
        return False, f"расширение сообщило нечитаемую версию {received!r}"
    if received == PROTOCOL_VERSION:
        return True, f"v{PROTOCOL_VERSION} = v{received} (сервер = расширение)"
    return False, (
        f"сервер v{PROTOCOL_VERSION}, расширение v{received}; обновите вторую "
        "сторону: расширение — git pull и Reload в chrome://extensions, CLI — "
        "pip install -e ."
    )


def check_allowlist(hello: dict | None) -> tuple[bool, str]:
    """(4) Allowlist действий сервера и расширения совпадает полностью."""
    if hello is None:
        return False, "нет handshake-данных — расширение не подключилось (см. п.2)"
    actions = hello.get("actions")
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        return False, f"расширение сообщило нечитаемый список действий {actions!r}"
    server_side = set(ALLOWED_ACTIONS)
    ext_side = set(actions)
    if server_side == ext_side:
        joined = ", ".join(sorted(ext_side))
        return True, f"действия совпадают с обеих сторон ({len(ext_side)}: {joined})"
    parts = []
    ext_only = sorted(ext_side - server_side)
    if ext_only:
        parts.append(
            f"расширение объявляет, а сервер не знает: {', '.join(ext_only)} — "
            "дополните ALLOWED_ACTIONS в src/hhru_bot/live/allowlist.py (валидаторы "
            "payload — как в content.js/executor.js)"
        )
    server_only = sorted(server_side - ext_side)
    if server_only:
        parts.append(
            f"сервер знает, а расширение не объявляет: {', '.join(server_only)} — "
            "перезагрузите актуальную версию расширения (chrome://extensions)"
        )
    return False, "наборы действий различаются; " + "; ".join(parts)


def check_permissions(hello: dict | None) -> tuple[bool, str]:
    """(5) Permissions манифеста ровно = ожидаемому минимуму (обе стороны)."""
    if hello is None:
        return False, "нет handshake-данных — расширение не подключилось (см. п.2)"
    perms = hello.get("permissions")
    host = hello.get("hostPermissions")
    if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
        return False, f"расширение сообщило нечитаемый список permissions {perms!r}"
    if not isinstance(host, list) or not all(isinstance(h, str) for h in host):
        return False, f"расширение сообщило нечитаемый список host_permissions {host!r}"
    parts = []
    extra = sorted(set(perms) - EXPECTED_PERMISSIONS)
    if extra:
        parts.append(f"лишние permissions (не минимальны): {', '.join(extra)}")
    missing = sorted(EXPECTED_PERMISSIONS - set(perms))
    if missing:
        parts.append(f"отсутствуют permissions (недостаточны): {', '.join(missing)}")
    host_extra = sorted(set(host) - EXPECTED_HOST_PERMISSIONS)
    if host_extra:
        parts.append(f"лишние host_permissions (не минимальны): {', '.join(host_extra)}")
    host_missing = sorted(EXPECTED_HOST_PERMISSIONS - set(host))
    if host_missing:
        parts.append(f"отсутствуют host_permissions (недостаточны): {', '.join(host_missing)}")
    if not parts:
        joined = ", ".join(sorted(perms))
        host_joined = ", ".join(sorted(host))
        return True, f"минимальны и достаточны (permissions: {joined}; host: {host_joined})"
    return (
        False,
        "; ".join(parts)
        + " — сверьте extensions/hhru-live/manifest.json и перезагрузите расширение",
    )


def _wait_hello(server: LiveServeServer, wait_seconds: float) -> dict | None:
    """Поднять serve() в потоке и ждать handshake расширения.

    stdin сервера — пайп, в который doctor никогда не пишет: закрытие write-end
    даёт EOF и гасит цикл (foreground-семантика #1159, фонового демона нет).
    Возвращает последний hello или None по таймауту.
    """
    read_fd, write_fd = os.pipe()
    thread = threading.Thread(target=server.serve, args=(read_fd, sys.stdout), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and server.client_hello is None:
            time.sleep(0.1)
        return server.client_hello
    finally:
        os.close(write_fd)
        thread.join(timeout=5)
        os.close(read_fd)


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "live-doctor",
        help=(
            "Диагностика live-канала: сервер, расширение, версия протокола, "
            "allowlist, permissions (READ-локально, без мутаций)"
        ),
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help=(
            "Порт сервера на 127.0.0.1 (по умолчанию 8765 — единственный, "
            "к которому расширение подключается само)"
        ),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        help="Сколько секунд ждать подключения расширения",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool | CommandExitCode:
    failed = False

    # (1) Сервер поднимается и слушает.
    server: LiveServeServer | None = LiveServeServer(port=args.port)
    try:
        server.bind()
    except OSError as exc:
        server = None
        print(
            f"[FAIL] сервер: не удалось занять 127.0.0.1:{args.port}: {exc}; "
            "подсказка: порт держит другая live-команда (live-serve/bump-live) — "
            "остановите её или повторите с --port <свободный>"
        )
        failed = True
    else:
        print(f"[OK] сервер: слушает {server.url} (протокол v{PROTOCOL_VERSION})")

    # (2) Расширение подключается (handshake hello — её доказательство).
    hello: dict | None = None
    if server is not None:
        print(
            f"[INFO] live-doctor: жду расширение hhru-live (до {args.wait_seconds:.0f} с) — "
            "нужен Chrome с включённым расширением"
        )
        hello = _wait_hello(server, args.wait_seconds)
        if hello is not None:
            print("[OK] расширение: подключено, handshake получен")
        elif server.connections_seen > 0:
            # Подключение было, hello нет: расширение из ревизии ДО #1163 —
            # транспорт жив, диагностику сообщать нечем (fail-closed).
            print(
                "[FAIL] расширение: подключилось, но handshake (hello) не получен — "
                "старая версия расширения; подсказка: git pull и Reload в "
                "chrome://extensions"
            )
            failed = True
        else:
            print(
                f"[FAIL] расширение: не подключилось за {args.wait_seconds:.0f} с; "
                f"подсказка: {_CONNECT_HINT}"
            )
            failed = True
    else:
        print(
            f"[FAIL] расширение: проверка пропущена — сервер не запущен; подсказка: {_CONNECT_HINT}"
        )
        failed = True

    # (3)-(5) Сверки handshake-диагностики; hello=None даёт честный FAIL каждой.
    for name, check in (
        ("версия протокола", check_version),
        ("allowlist", check_allowlist),
        ("permissions", check_permissions),
    ):
        ok, message = check(hello)
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {message}")
        failed |= not ok

    verdict = "все проверки пройдены" if not failed else "есть [FAIL] — см. подсказки выше"
    print(f"Итог: {verdict}")
    return failed

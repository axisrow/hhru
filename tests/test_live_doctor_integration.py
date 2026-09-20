"""live-doctor end-to-end: run() на реальном loopback-сервере с клиентом-
заглушкой, шлющей handshake hello (#1163). Расширение не загружается."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import threading
import time

import pytest

from hhru_bot.commands import live_doctor
from hhru_bot.live import ALLOWED_ACTIONS, PROTOCOL_VERSION

pytestmark = pytest.mark.integration


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class HelloClient:
    """Минимальный WS-клиент: handshake + один маскированный text-кадр."""

    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.setblocking(True)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                "GET / HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)

    def send_hello(self, hello: dict) -> None:
        payload = json.dumps(hello).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        else:
            header.append(0x80 | 126)
            header += struct.pack(">H", len(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def close(self) -> None:
        self.sock.close()


def _hello() -> dict:
    return {
        "kind": "hello",
        "v": PROTOCOL_VERSION,
        "actions": sorted(ALLOWED_ACTIONS),
        "permissions": sorted(live_doctor.EXPECTED_PERMISSIONS),
        "hostPermissions": sorted(live_doctor.EXPECTED_HOST_PERMISSIONS),
    }


def _doctor_args(port: int, wait: float = 5.0) -> argparse.Namespace:
    return argparse.Namespace(port=port, wait_seconds=wait)


def _run_with_client(port: int, hello: dict | None, wait: float = 5.0) -> bool:
    """run() в потоке; клиент подключается из тестового потока, как Chrome."""
    outcome: dict = {}

    def target() -> None:
        outcome["failed"] = live_doctor.run(_doctor_args(port, wait))

    thread = threading.Thread(target=target)
    thread.start()
    client = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            client = HelloClient(port)
            break
        except OSError:
            time.sleep(0.05)
    assert client is not None, "сервер doctor'а не поднялся за 5 с"
    if hello is not None:
        client.send_hello(hello)
    thread.join(timeout=wait + 15)
    assert not thread.is_alive(), "live-doctor не завершился"
    client.close()
    return outcome["failed"]


def test_healthy_handshake_passes_all_checks(capsys):
    port = _free_port()
    failed = _run_with_client(port, _hello())
    out = capsys.readouterr().out
    assert failed is False
    for line in (
        "[OK] сервер:",
        "[OK] расширение: подключено, handshake получен",
        "[OK] версия протокола:",
        "[OK] allowlist:",
        "[OK] permissions:",
        "Итог: все проверки пройдены",
    ):
        assert line in out, f"нет строки {line!r} в выводе:\n{out}"


def test_version_mismatch_fails_only_that_check(capsys):
    port = _free_port()
    failed = _run_with_client(port, {**_hello(), "v": PROTOCOL_VERSION + 1})
    out = capsys.readouterr().out
    assert failed is True
    assert "[FAIL] версия протокола:" in out
    assert "[OK] allowlist:" in out
    assert "[OK] permissions:" in out


def test_allowlist_drift_fails_allowlist_check(capsys):
    port = _free_port()
    failed = _run_with_client(port, {**_hello(), "actions": ["extra_action"]})
    out = capsys.readouterr().out
    assert failed is True
    assert "[FAIL] allowlist:" in out
    assert "extra_action" in out
    assert "[OK] версия протокола:" in out


def test_connected_without_hello_is_fail_closed(capsys):
    """Клиент из ревизии до #1163: подключение есть, hello нет — честный FAIL."""
    port = _free_port()
    failed = _run_with_client(port, None, wait=1.0)
    out = capsys.readouterr().out
    assert failed is True
    assert "[OK] сервер:" in out
    assert "handshake (hello) не получен" in out
    assert "[FAIL] версия протокола:" in out


def test_no_client_at_all_fails_with_hint(capsys):
    port = _free_port()
    failed = live_doctor.run(_doctor_args(port, wait=0.3))
    out = capsys.readouterr().out
    assert failed is True
    assert "не подключилось за 0 с" in out
    assert "chrome://extensions" in out

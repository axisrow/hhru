"""live-serve: loopback WS-сервер end-to-end на реальных сокетах (#1159).

Loopback 127.0.0.1 — не внешняя сеть: сервер, тестовый WS-клиент (ручной
RFC 6455, роль будущего клиента-расширения S2) и stdin-пайп живут в одном
процессе. Браузер не запускается.
"""

from __future__ import annotations

import base64
import json
import os
import select
import socket
import struct
import threading
import time
from typing import Any

import pytest

from hhru_bot.live import PROTOCOL_VERSION, LiveServeServer

pytestmark = pytest.mark.integration

OP_TEXT = 0x1
OP_PING = 0x9
OP_PONG = 0xA

FAST = {
    "heartbeat_interval": 0.15,
    # Таймаут ответа должен сработать РАНЬШЕ heartbeat-дропа (3*0.15=0.45),
    # иначе команда будет классифицирована client_disconnected, а не timeout.
    "response_timeout": 0.3,
}


class ExtensionClientStub:
    """Минимальный WS-клиент: handshake, маскированные фреймы, close."""

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
        self._read_handshake_response()

    def _read_handshake_response(self) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            data = self.sock.recv(4096)
            assert data, "сервер закрыл соединение до handshake"
            buf += data
        assert buf.startswith(b"HTTP/1.1 101"), buf[:64]

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        else:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_frame(self, timeout: float = 3.0) -> tuple[int, bytes]:
        """Вернуть (opcode, payload) ближайшего НЕ-ping фрейма.

        Пинг внутри отвечает pong'ом (роль будущей WS-библиотеки клиента);
        если данных нет за timeout — socket.timeout/TimeoutError. Один читатель
        на сокет: никакой фоновой нити, которая могла бы вычитать чужой фрейм.
        """
        deadline = time.monotonic() + timeout
        while True:
            opcode, payload = self._read_frame(deadline)
            if opcode == OP_PING:
                self.send_pong(payload)
                continue
            return opcode, payload

    def send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x8A, 0x80 | len(payload)])
        self.sock.sendall(bytes(header) + mask + masked)

    def _read_frame(self, deadline: float) -> tuple[int, bytes]:
        def read_exact(n: int) -> bytes:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            buf = b""
            while len(buf) < n:
                data = self.sock.recv(n - len(buf))
                if not data:
                    raise ConnectionError("сервер закрыл соединение")
                buf += data
            return buf

        head = read_exact(2)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", read_exact(8))[0]
        if head[1] & 0x80:
            read_exact(4)  # серверные фреймы не маскируются, но пропарсим честно
        return opcode, read_exact(length) if length else b""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class ServerHarness:
    """Сервер в потоке + stdin-пайп + буфер stdout с ожиданием строк."""

    def __init__(self, **server_kwargs: Any) -> None:
        opts = {**FAST, **server_kwargs}
        self.server = LiveServeServer(**opts)
        self.server.bind()
        self.stdin_read, self.stdin_write = os.pipe()
        self.out_read, self.out_write = os.pipe()
        self._out_buf = ""
        self._stopped = False
        self._stdin_closed = False
        # daemon: упавший без stop() тест не должен держать процесс pytest.
        self.thread = threading.Thread(
            target=self.server.serve,
            args=(self.stdin_read, os.fdopen(self.out_write, "w")),
            daemon=True,
        )
        self.thread.start()

    def send_stdin(self, text: str) -> None:
        os.write(self.stdin_write, (text + "\n").encode("utf-8"))

    def close_stdin(self) -> None:
        if self._stdin_closed:
            return
        self._stdin_closed = True
        os.close(self.stdin_write)

    def lines(self) -> list[str]:
        """Неблокирующе дочитать новый вывод и вернуть ПОЛНЫЕ строки."""
        while select.select([self.out_read], [], [], 0)[0]:
            chunk = os.read(self.out_read, 65536).decode("utf-8")
            if not chunk:
                break
            self._out_buf += chunk
        if "\n" not in self._out_buf:
            return []
        drained, self._out_buf = self._out_buf.rsplit("\n", 1)
        return [line for line in drained.split("\n") if line]

    def wait_for_line(self, predicate, timeout: float = 5.0) -> str:
        """Ждать строку, удовлетворяющую предикату; вернуть её.

        Имя не ``wait_for``: тестовый страж контракта фейков Playwright
        (test_playwright_fakes_contract) сверяет такие методы с Locator API,
        а этот хелпер — про строки stdout сервера, не про страницу.
        """
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            for line in self.lines():
                seen.append(line)
                if predicate(line):
                    return line
            time.sleep(0.02)
        raise AssertionError(f"строка не найдена за {timeout}s; вывод: {seen}")

    def stop(self) -> None:
        if self._stopped:
            return
        self.close_stdin()
        self._stopped = True
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "serve() не завершился после EOF stdin"
        os.close(self.stdin_read)
        os.close(self.out_read)


def _json_response_line(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and "status" in obj else None


def _has_code(code: str):
    """Предикат wait_for: JSON-ответ с transport-кодом code."""

    def predicate(line: str) -> bool:
        obj = _json_response_line(line)
        return obj is not None and obj.get("result", {}).get("code") == code

    return predicate


def _envelope(id_: str, action: str, payload: dict | None = None) -> str:
    return json.dumps(
        {
            "v": PROTOCOL_VERSION,
            "id": id_,
            "action": action,
            "payload": payload or {},
        }
    )


@pytest.fixture()
def server_harness():
    harness = ServerHarness()
    yield harness
    harness.stop()


def test_command_roundtrip_through_real_socket(server_harness):
    client = ExtensionClientStub(server_harness.server.port)
    try:
        server_harness.wait_for_line(lambda line: "подключ" in line or "client" in line)

        server_harness.send_stdin(_envelope("c1", "list_overlays"))
        opcode, payload = client.recv_frame()
        assert opcode == OP_TEXT
        forwarded = json.loads(payload.decode("utf-8"))
        assert forwarded == {
            "v": PROTOCOL_VERSION,
            "id": "c1",
            "action": "list_overlays",
            "payload": {},
        }

        # Ответ расширения пробрасывается дословно (чужой код ошибки — данные).
        client.send_text(
            json.dumps({"id": "c1", "status": "error", "result": {"code": "action_not_allowed"}})
        )
        line = server_harness.wait_for_line(_json_response_line)
        assert json.loads(line) == {
            "id": "c1",
            "status": "error",
            "result": {"code": "action_not_allowed"},
        }
    finally:
        client.close()


def test_no_client_is_explicit_error_not_queueing(server_harness):
    server_harness.send_stdin(_envelope("c1", "check_element", {"selector": "div"}))
    line = server_harness.wait_for_line(_has_code("no_client"))
    resp = json.loads(line)
    assert (resp["id"], resp["status"]) == ("c1", "error")


def _assert_no_text_frame(client: ExtensionClientStub, window: float) -> None:
    """За окно window клиенту не приходит ни одного TEXT-кадра.

    Ping/close/pong допускаются (ping отвечает pong'ом внутри recv_frame);
    проверяется именно отсутствие пересылки данных исполнителю.
    """
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        try:
            opcode, _payload = client.recv_frame(timeout=max(0.05, deadline - time.monotonic()))
        except TimeoutError:
            return
        assert opcode != OP_TEXT, "команда вне allowlist/версии дошла до исполнителя"


def test_unknown_action_and_version_never_reach_client():
    harness = ServerHarness()
    client = ExtensionClientStub(harness.server.port)
    try:
        harness.send_stdin(_envelope("c1", "self_destruct"))
        line = harness.wait_for_line(_has_code("unknown_action"))
        assert json.loads(line)["id"] == "c1"
        harness.send_stdin(json.dumps({"v": 99, "id": "c2", "action": "list_overlays"}))
        line = harness.wait_for_line(_has_code("unsupported_version"))
        assert json.loads(line)["id"] == "c2"
        # Fail-closed: исполнителю в браузере не ушло ничего.
        _assert_no_text_frame(client, 0.5)
    finally:
        client.close()
        harness.stop()


@pytest.mark.parametrize(
    "raw,code",
    [
        ("{not json", "bad_envelope"),
        ("[1]", "bad_envelope"),
        (json.dumps({"v": PROTOCOL_VERSION, "action": "list_overlays"}), "bad_envelope"),
        (json.dumps({"id": "c1", "action": "list_overlays"}), "bad_envelope"),
    ],
)
def test_malformed_envelope_rejected_without_client(server_harness, raw, code):
    server_harness.send_stdin(raw)
    line = server_harness.wait_for_line(_json_response_line)
    resp = json.loads(line)
    assert resp["result"]["code"] == code
    assert resp["status"] == "error"


def test_response_timeout_is_explicit():
    harness = ServerHarness()
    client = ExtensionClientStub(harness.server.port)
    try:
        harness.send_stdin(_envelope("c1", "list_overlays"))
        client.recv_frame()  # команда дошла, расширение молчит
        line = harness.wait_for_line(_has_code("timeout"))
        resp = json.loads(line)
        assert (resp["id"], resp["status"]) == ("c1", "error")
    finally:
        client.close()
        harness.stop()


def test_client_disconnect_while_pending_fails_the_command():
    harness = ServerHarness()
    client = ExtensionClientStub(harness.server.port)
    harness.send_stdin(_envelope("c1", "list_overlays"))
    client.recv_frame()
    client.close()
    line = harness.wait_for_line(lambda line: "client_disconnected" in line)
    assert json.loads(line)["id"] == "c1"
    harness.stop()


def test_silent_client_dropped_by_heartbeat():
    harness = ServerHarness()
    client = ExtensionClientStub(harness.server.port)
    # Не отвечаем на ping: сервер обязан отличить «мёртвый» сокет.
    deadline_line = harness.wait_for_line(lambda line: "heartbeat" in line, timeout=5)
    assert "[INFO]" in deadline_line
    harness.send_stdin(_envelope("c1", "list_overlays"))
    line = harness.wait_for_line(_has_code("no_client"))
    assert json.loads(line)["result"]["code"] == "no_client"
    client.close()
    harness.stop()


def test_heartbeat_ping_answered_by_pong_keeps_client_alive():
    harness = ServerHarness()
    client = ExtensionClientStub(harness.server.port)
    # > heartbeat_timeout (0.45s) опрашиваем сокет: recv_frame отвечает на
    # пинги pong'ами, и дропа «нет ответов» быть не должно.
    alive_deadline = time.monotonic() + 0.6
    while time.monotonic() < alive_deadline:
        try:
            client.recv_frame(timeout=0.1)
        except TimeoutError:
            continue
    harness.send_stdin(_envelope("c1", "list_overlays"))
    opcode, payload = client.recv_frame()
    assert opcode == OP_TEXT
    assert json.loads(payload)["id"] == "c1"
    client.close()
    harness.stop()


def test_reconnect_after_disconnect_is_normal():
    harness = ServerHarness()
    first = ExtensionClientStub(harness.server.port)
    first.close()
    harness.wait_for_line(lambda line: "отключён" in line or "disconnect" in line)

    second = ExtensionClientStub(harness.server.port)
    try:
        harness.send_stdin(_envelope("c9", "list_overlays"))
        opcode, payload = second.recv_frame()
        assert opcode == OP_TEXT
        assert json.loads(payload)["id"] == "c9"
        second.send_text(json.dumps({"id": "c9", "status": "ok", "result": {"overlays": []}}))
        line = harness.wait_for_line(_json_response_line)
        assert json.loads(line)["status"] == "ok"
    finally:
        second.close()
        harness.stop()


def test_second_connection_rejected_while_first_active():
    harness = ServerHarness()
    first = ExtensionClientStub(harness.server.port)
    try:
        intruder = socket.create_connection(("127.0.0.1", harness.server.port), timeout=5)
        # Без handshake: сервер закрывает лишний сокет, не занимая слот.
        deadline = time.monotonic() + 3
        intruder.settimeout(0.2)
        closed = False
        while time.monotonic() < deadline:
            try:
                if intruder.recv(1) == b"":
                    closed = True
                    break
            except TimeoutError:
                continue
        intruder.close()
        assert closed, "второе подключение не было закрыто"
        # Первый клиент продолжает работать.
        harness.send_stdin(_envelope("c1", "list_overlays"))
        opcode, payload = first.recv_frame()
        assert json.loads(payload)["id"] == "c1"
    finally:
        first.close()
        harness.stop()


def test_bad_response_shape_from_client_is_error():
    harness = ServerHarness()
    client = ExtensionClientStub(harness.server.port)
    try:
        harness.send_stdin(_envelope("c1", "list_overlays"))
        client.recv_frame()
        client.send_text('{"nope": 1}')
        line = harness.wait_for_line(_json_response_line)
        resp = json.loads(line)
        assert resp["id"] == "c1"
        assert resp["result"]["code"] == "bad_response"
    finally:
        client.close()
        harness.stop()


def test_unsolicited_client_message_gets_explicit_error():
    harness = ServerHarness()
    client = ExtensionClientStub(harness.server.port)
    try:
        client.send_text(json.dumps({"id": "x1", "status": "ok", "result": {}}))
        line = harness.wait_for_line(_json_response_line)
        resp = json.loads(line)
        assert resp["id"] == "x1"
        assert resp["result"]["code"] == "unexpected_message"
    finally:
        client.close()
        harness.stop()


def test_eof_stdin_stops_foreground_server(server_harness):
    client = ExtensionClientStub(server_harness.server.port)
    server_harness.send_stdin(_envelope("c1", "list_overlays"))
    client.recv_frame()
    server_harness.close_stdin()
    # in-flight команда при EOF не брошена: ответ клиента принимается до конца,
    # сервер гасится уже после её завершения (join живёт в stop()).
    client.send_text(json.dumps({"id": "c1", "status": "ok", "result": {}}))
    line = server_harness.wait_for_line(_json_response_line, timeout=2)
    assert json.loads(line)["status"] == "ok"
    client.close()


def test_send_failure_drops_client_and_frees_slot():
    harness = ServerHarness()
    client = ExtensionClientStub(harness.server.port)
    try:
        # Клиентская сторона handshake завершается раньше серверного accept'а:
        # под нагрузкой (xdist) select-цикл ещё не занял слот, и _client None.
        harness.wait_for_line(lambda line: "подключ" in line)
        # Симуляция частичной записи: send_text падает, битый клиент не
        # должен остаться в слоте (следующая команда ушла бы в мусор).
        def _broken_send(text, timeout=10.0):
            raise OSError("битый поток фреймов")

        harness.server._client.send_text = _broken_send
        harness.send_stdin(_envelope("c1", "list_overlays"))
        line = harness.wait_for_line(_has_code("client_disconnected"))
        assert json.loads(line)["id"] == "c1"
        # Слот свободен: следующая команда получает no_client, а не уходит
        # в битый поток ([INFO] об отключении уже вычитан первым wait'ом).
        harness.send_stdin(_envelope("c2", "list_overlays"))
        assert harness.wait_for_line(_has_code("no_client"))
    finally:
        client.close()
        harness.stop()


def test_commands_are_single_flight():
    # response_timeout больше окна тишины: pending c1 не должен успеть
    # истечь, иначе сервер штатно переслал бы c2 уже после таймаута.
    harness = ServerHarness(response_timeout=5.0)
    client = ExtensionClientStub(harness.server.port)
    try:
        # Две команды одной пачкой: вторая не должна перетереть pending первой.
        os.write(
            harness.stdin_write,
            (
                _envelope("c1", "list_overlays") + "\n" + _envelope("c2", "list_overlays") + "\n"
            ).encode(),
        )
        first_opcode, first_payload = client.recv_frame()
        assert json.loads(first_payload)["id"] == "c1"
        # Пока c1 не отвечена, c2 не пересылается.
        _assert_no_text_frame(client, 0.5)
        client.send_text(json.dumps({"id": "c1", "status": "ok", "result": {}}))
        line = harness.wait_for_line(_json_response_line)
        assert json.loads(line)["id"] == "c1"
        _, second_payload = client.recv_frame()
        assert json.loads(second_payload)["id"] == "c2"
    finally:
        client.close()
        harness.stop()


# ---------------------------------------------------------------------------
# kind-кадры — диагностика клиента, не ответы на команды (#1163): hello
# сохраняется для live-doctor, heartbeat молчит (не bad_response-спам).
# ---------------------------------------------------------------------------

_HELLO = {
    "kind": "hello",
    "v": PROTOCOL_VERSION,
    "actions": ["check_element", "list_overlays"],
    "permissions": ["storage"],
    "hostPermissions": ["https://hh.ru/*"],
}


def test_hello_frame_stored_as_diagnostics_not_error(server_harness):
    client = ExtensionClientStub(server_harness.server.port)
    try:
        client.send_text(json.dumps(_HELLO))
        time.sleep(0.2)  # окно, в котором старое поведение дало бы bad_response
        assert server_harness.server.client_hello == _HELLO
        # Ни одной JSON-строки ответа в stdout — диагностика не ошибка.
        assert all(_json_response_line(line) is None for line in server_harness.lines())
    finally:
        client.close()


def test_heartbeat_frames_are_silent_and_keep_connection(server_harness):
    client = ExtensionClientStub(server_harness.server.port)
    try:
        client.send_text(json.dumps({"kind": "heartbeat", "observedAt": "2026-09-20T00:00:00Z"}))
        time.sleep(0.2)
        assert server_harness.server.client_hello is None
        # Соединение живо: команда доходит и отвечается как обычно.
        server_harness.send_stdin(_envelope("c1", "list_overlays"))
        opcode, payload = client.recv_frame()
        assert json.loads(payload)["id"] == "c1"
        client.send_text(json.dumps({"id": "c1", "status": "ok", "result": {}}))
        assert json.loads(server_harness.wait_for_line(_json_response_line))["status"] == "ok"
    finally:
        client.close()


def test_reconnect_hello_overwrites_previous(server_harness):
    first = ExtensionClientStub(server_harness.server.port)
    first.send_text(json.dumps(_HELLO))
    time.sleep(0.2)
    first.close()
    server_harness.wait_for_line(lambda line: "отключён" in line)

    second_hello = {**_HELLO, "actions": ["check_element"]}
    second = ExtensionClientStub(server_harness.server.port)
    try:
        second.send_text(json.dumps(second_hello))
        time.sleep(0.2)
        assert server_harness.server.client_hello == second_hello
    finally:
        second.close()

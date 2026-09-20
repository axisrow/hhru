"""Минимальный серверный WebSocket (RFC 6455) на stdlib-сокетах (#1159).

Зачем свой: зависимость websockets ради одного loopback-клиента избыточна,
а нужный срез протокола узкий — handshake + текстовые фреймы + ping/pong/close,
один клиент, маленькие JSON-сообщения. Всё остальное не поддерживается и
fail-closed: фрагментация, binary-фреймы, немаскированные клиентские фреймы,
сообщения больше MAX_MESSAGE_BYTES — WSError и разрыв соединения (клиент
этого канала — наше расширение hhru-live, оно не порождает таких фреймов).

Байтовый транспорт неблокирующий: все чтения/записи идут через select с
дедлайном, чтобы владелец соединения (live/server.py) мог держать heartbeat
и таймауты ответов на одном потоке.
"""

from __future__ import annotations

import base64
import hashlib
import select
import socket
import struct
import time

# Секрет handshake из RFC 6455 §1.3.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_MESSAGE_BYTES = 1 << 20
MAX_HANDSHAKE_BYTES = 8192

CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_TOO_BIG = 1009


class WSError(Exception):
    """Нарушение WS-протокола или обрыв соединения — соединение закрывается."""

    def __init__(self, detail: str, *, close_code: int = CLOSE_PROTOCOL_ERROR) -> None:
        super().__init__(detail)
        self.close_code = close_code


class WSConnection:
    """Одно серверное соединение с клиентом-расширением."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        sock.setblocking(False)
        # Недочитанный handshake-запрос (#1185): дозревает по неблокирующим
        # вызовам advance_handshake, пока владелец гоняет свой select-цикл.
        self._hs_buf = bytearray()
        self._handshake_done = False

    @property
    def sock(self) -> socket.socket:
        return self._sock

    # -- handshake ---------------------------------------------------------

    def advance_handshake(self, deadline: float) -> bool:
        """Один неблокирующий шаг handshake (GET + Upgrade), #1185.

        Вызывается, когда сокет стал читаемым (select-цикл владельца);
        handshake-запрос дочитывается по мере поступления байт, ответ 101
        уходит сразу после полного запроса. Дедлайн (monotonic, абсолютный)
        держит вызывающий и в него же упирает ожидание записи ответа.

        True — handshake завершён, соединение готово к recv_message.
        WSError — нарушение протокола или обрыв: соединение закрывает
        вызывающий. Байты после ``\\r\\n\\r\\n`` отбрасываются (как и в
        прежнем блокирующем accept'е): наш клиент шлёт кадры только после 101.
        """
        if self._handshake_done:
            return True
        try:
            # +1: первый байт ЗА лимитом делает переполнение различимым —
            # без него буфер физически не может превысить MAX, ветка ниже
            # мертва, а 8193 байта без \r\n\r\n ловились бы как «закрыто
            # до handshake» или молча по дедлайну (ревью PR #1202).
            data = self._sock.recv(MAX_HANDSHAKE_BYTES + 1 - len(self._hs_buf))
        except (BlockingIOError, InterruptedError):
            return False
        except OSError as exc:
            raise WSError(f"обрыв при handshake: {exc}", close_code=CLOSE_NORMAL) from exc
        if not data:
            raise WSError("соединение закрыто до handshake", close_code=CLOSE_NORMAL)
        self._hs_buf += data
        if len(self._hs_buf) > MAX_HANDSHAKE_BYTES:
            raise WSError("handshake-запрос слишком большой")
        if b"\r\n\r\n" not in self._hs_buf:
            return False
        request, self._hs_buf = bytes(self._hs_buf), bytearray()
        headers = _parse_headers(request)
        if headers.get("upgrade", "").lower() != "websocket":
            raise WSError("нет Upgrade: websocket")
        key = headers.get("sec-websocket-key")
        if not key:
            raise WSError("нет Sec-WebSocket-Key")
        accept_hash = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        accept = base64.b64encode(accept_hash).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        # ponytail: loopback, ~120 байт в пустой send-буфер — select в
        # _send_all возвращает мгновенно; клиент, читающий ответ 101,
        # остаётся обязанностью вызывающего через дедлайн.
        self._send_all(response.encode("ascii"), max(0.0, deadline - time.monotonic()))
        self._handshake_done = True
        return True

    # -- приём -------------------------------------------------------------

    def recv_message(self, deadline: float) -> tuple[str, str | bytes]:
        """Читать до полной значимой записи.

        Возвращает ("text"|"close"|"pong", payload); WS-ping отвечает pong'ом
        внутри и наружу не пробрасывается. Pong возвращается вызывающему
        отдельным kind — тот обновляет свежесть соединения, но содержимого у
        pong нет. Дедлайн в секундах monotonic; истечение — WSError с
        close_code=CLOSE_NORMAL (это таймаут, а не нарушение протокола).
        """
        while True:
            head = self._recv_exact(2, deadline)
            fin = bool(head[0] & 0x80)
            opcode = head[0] & 0x0F
            if not head[1] & 0x80:
                # RFC 6455 §5.1: клиент обязан маскировать — иначе это не
                # наш клиент, соединение не продолжается.
                raise WSError("клиентский фрейм без маски")
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8, deadline))[0]
            if length > MAX_MESSAGE_BYTES:
                raise WSError("сообщение больше MAX_MESSAGE_BYTES", close_code=CLOSE_TOO_BIG)
            if opcode in (OP_PING, OP_PONG, OP_CLOSE) and (not fin or length > 125):
                raise WSError("контрольный фрейм фрагментирован или слишком длинный")
            mask = self._recv_exact(4, deadline)
            payload = bytearray(self._recv_exact(length, deadline))
            for i in range(length):
                payload[i] ^= mask[i % 4]

            if opcode == OP_PING:
                self.send_frame(OP_PONG, bytes(payload))
                continue
            if opcode == OP_PONG:
                return "pong", bytes(payload)
            if opcode == OP_CLOSE:
                return "close", bytes(payload)
            if opcode == OP_TEXT:
                if not fin:
                    raise WSError("фрагментация не поддерживается")
                try:
                    return "text", bytes(payload).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WSError(f"текст не UTF-8: {exc}") from exc
            raise WSError(f"неподдерживаемый opcode {opcode}")

    # -- отправка ----------------------------------------------------------

    def send_frame(self, opcode: int, payload: bytes, timeout: float = 10.0) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 1 << 16:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        # Серверные фреймы не маскируются (RFC 6455 §5.1).
        self._send_all(bytes(header) + payload, timeout)

    def send_text(self, text: str, timeout: float = 10.0) -> None:
        self.send_frame(OP_TEXT, text.encode("utf-8"), timeout)

    def send_ping(self, timeout: float = 10.0) -> None:
        self.send_frame(OP_PING, b"", timeout)

    def send_close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        # Best-effort: ошибка отправки close не должна маскировать причину
        # разрыва, соединение всё равно закрывается вызывающим.
        try:
            self.send_frame(OP_CLOSE, struct.pack(">H", code) + reason.encode("utf-8")[:120])
        except (WSError, OSError):
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    # -- байтовый транспорт --------------------------------------------------

    def _recv_exact(self, n: int, deadline: float) -> bytes:
        chunks = bytearray()
        while len(chunks) < n:
            _wait_readable(self._sock, deadline)
            try:
                data = self._sock.recv(n - len(chunks))
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise WSError(f"обрыв соединения: {exc}", close_code=CLOSE_NORMAL) from exc
            if not data:
                raise WSError("соединение закрыто клиентом", close_code=CLOSE_NORMAL)
            chunks += data
        return bytes(chunks)

    def _send_all(self, data: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        view = memoryview(data)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WSError("таймаут записи", close_code=CLOSE_NORMAL)
            _, writable, _ = select.select([], [self._sock], [], remaining)
            if not writable:
                continue
            try:
                sent = self._sock.send(view)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise WSError(f"обрыв соединения: {exc}", close_code=CLOSE_NORMAL) from exc
            view = view[sent:]


def _wait_readable(sock: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WSError("таймаут чтения", close_code=CLOSE_NORMAL)
    readable, _, _ = select.select([sock], [], [], remaining)
    if not readable:
        raise WSError("таймаут чтения", close_code=CLOSE_NORMAL)


def _parse_headers(raw: bytes) -> dict[str, str]:
    lines = raw.decode("latin-1").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers

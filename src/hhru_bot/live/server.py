"""Loopback WS-сервер live-канала (#1159, этап 2 эпика #588).

Топология:: агент (stdin/stdout CLI) --LiveServeServer--WS--> расширение
hhru-live в живой вкладке Chrome. Сервер единственный источник команд и
единственный читатель ответов; исполнителем остаётся браузер.

Инварианты ишью #1159:

- **Один клиент.** Подключение второго отклоняется немедленно (TCP close);
  переподключение того же клиента после обрыва — штатное событие, а не ошибка
  сценария.
- **Reconnect/heartbeat.** Разрыв соединения не останавливает сервер: пинг
  каждые ``heartbeat_interval``, клиент, молчащий дольше
  ``heartbeat_timeout``, объявляется потерянным («полуоткрытый» сокет не
  держит слот). Сервер отличает «нет клиента» (ошибка ``no_client``) от
  «клиент есть, но команда отказала/не ответила» (``timeout``,
  ``client_disconnected``, ответ расширения с его собственным кодом ошибки).
- **Fail-closed.** Битый envelope/версия/действие не пересылаются вовсе;
  команда без активного соединения не ставится в очередь, а получает явный
  ``no_client``; ответ с чужим id и незапрошенные сообщения клиента получают
  явную ошибку, не тишину.
- **Foreground.** Сервер живёт в select-цикле одного потока, пока открыт stdin
  вызывающего; EOF stdin или Ctrl+C гасят его. Фонового демона нет.
"""

from __future__ import annotations

import os
import select
import socket
import time
from typing import TextIO

from .protocol import (
    BAD_RESPONSE,
    CLIENT_DISCONNECTED,
    NO_CLIENT,
    PROTOCOL_VERSION,
    TIMEOUT,
    UNEXPECTED_MESSAGE,
    ProtocolError,
    error_response,
    parse_envelope,
    parse_response,
    serialize,
)
from .ws import WSConnection, WSError

DEFAULT_HEARTBEAT_INTERVAL = 15.0
# Три интервала без входящих кадров (пингов или данных) — клиент потерян.
HEARTBEAT_TIMEOUT_FACTOR = 3.0
# Бюджет ожидания ответа расширения на одну команду.
DEFAULT_RESPONSE_TIMEOUT = 30.0
# Бюджет на дочитывание одного WS-кадра, когда сокет уже стал читаемым
# (loopback: кадр приходит целиком за миллисекунды).
FRAME_TIMEOUT = 5.0
HANDSHAKE_TIMEOUT = 5.0


class LiveServeServer:
    """Loopback WS-сервер: stdin-команды -> расширение, ответы -> stdout."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_interval * HEARTBEAT_TIMEOUT_FACTOR
        self.response_timeout = response_timeout
        self._listener: socket.socket | None = None
        self._client: WSConnection | None = None
        # (command_id, deadline) единственной команды в полёте (single-flight).
        self._pending: tuple[str | int, float] | None = None
        self._last_seen = 0.0
        self._next_ping = 0.0
        self.connections_seen = 0
        self.commands_forwarded = 0

    # -- жизненный цикл ------------------------------------------------------

    def bind(self) -> None:
        """Занять порт. port=0 — ephemeral; фактический порт см. :attr:`port`."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(1)
        self._listener = listener
        self.host, self.port = listener.getsockname()[:2]

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def serve(self, command_fd: int, out: TextIO) -> None:
        """Главный цикл до EOF stdin. out — поток строк stdout команды."""
        if self._listener is None:
            raise RuntimeError("bind() не вызван")
        stdin_open = True
        stdin_buf = b""
        try:
            while True:
                now = time.monotonic()
                timeout = self._select_timeout(now)
                # Single-flight: пока команда в полёте, stdin не читаем —
                # следующая строка подождёт своей очереди в буфере пайпа.
                wait_fds: list[int | socket.socket] = [self._listener]
                if self._client is not None:
                    wait_fds.append(self._client.sock)
                if stdin_open and self._pending is None:
                    wait_fds.append(command_fd)
                try:
                    readable, _, _ = select.select(wait_fds, [], [], timeout)
                except InterruptedError:  # pragma: no cover - PEP 475 ретраит сам
                    continue
                now = time.monotonic()

                if self._listener in readable:
                    self._accept(out)
                if stdin_open and self._pending is None and command_fd in readable:
                    chunk = os.read(command_fd, 65536)
                    if chunk:
                        stdin_buf += chunk
                    else:
                        stdin_open = False
                if self._client is not None and self._client.sock in readable:
                    self._recv_client(now, out)
                # Порция строк обрабатывается всегда, когда сервер свободен:
                # и после нового куска, и после завершения предыдущей команды.
                if self._pending is None and stdin_buf:
                    if b"\n" in stdin_buf:
                        line, stdin_buf = stdin_buf.split(b"\n", 1)
                        self._handle_line(line.decode("utf-8", "replace"), now, out)
                    elif not stdin_open:
                        # Финальная строка без завершающего перевода — тоже команда.
                        line, stdin_buf = stdin_buf, b""
                        self._handle_line(line.decode("utf-8", "replace"), now, out)
                if not stdin_open and self._pending is None and not stdin_buf:
                    self._info(out, "stdin закрыт — сервер останавливается")
                    break
                self._check_deadlines(time.monotonic(), out)
        finally:
            if self._client is not None:
                self._client.send_close()
                self._client.close()
                self._client = None
            if self._listener is not None:
                self._listener.close()
                self._listener = None

    # -- подключения ---------------------------------------------------------

    def _accept(self, out: TextIO) -> None:
        listener = self._listener
        if listener is None:  # pragma: no cover - защищает тип
            return
        if self._client is not None:
            # Слот один: «лишний» сокет закрывается без handshake — клиент
            # увидит обрыв и повторит по своему reconnect-циклу.
            try:
                sock, _ = listener.accept()
                sock.close()
            except OSError:
                pass
            return
        try:
            self._client = WSConnection.accept(listener, HANDSHAKE_TIMEOUT)
        except (WSError, OSError):
            # Не-handshake-подключение (скан порта, GET в браузере) — не ошибка
            # сценария; слушаем дальше.
            return
        self.connections_seen += 1
        self._last_seen = time.monotonic()
        self._next_ping = self._last_seen + self.heartbeat_interval
        self._info(out, "клиент подключён")

    def _recv_client(self, now: float, out: TextIO) -> None:
        client = self._client
        if client is None:  # pragma: no cover - защищает тип
            return
        try:
            kind, payload = client.recv_message(deadline=now + FRAME_TIMEOUT)
        except WSError as exc:
            self._drop_client(f"соединение потеряно ({exc})", out)
            return
        self._last_seen = now
        if kind == "pong":
            return
        if kind == "close":
            self._drop_client("клиент закрыл соединение", out)
            return
        try:
            response = parse_response(payload)
        except ProtocolError as exc:
            if self._pending is not None:
                # Ждали ответ на команду, получили мусор — честный отказ
                # немедленно, без выжидания таймаута.
                pending_id, _ = self._pending
                self._pending = None
                self._emit(out, error_response(pending_id, BAD_RESPONSE, exc.detail))
            else:
                self._emit(out, error_response(None, BAD_RESPONSE, exc.detail))
            return
        if self._pending is not None and response["id"] == self._pending[0]:
            self._pending = None
            self._emit(out, response)
            return
        # Чужой id при незакрытой команде или незапрошенное сообщение —
        # fail-closed: явная ошибка, состояние ожидания не трогаем.
        self._emit(
            out,
            error_response(
                response["id"],
                UNEXPECTED_MESSAGE,
                "ответ не относится к команде в полёте"
                if self._pending is not None
                else "сообщение без команды в полёте",
            ),
        )

    def _drop_client(self, reason: str, out: TextIO) -> None:
        client = self._client
        self._client = None
        if client is not None:
            client.send_close()
            client.close()
        if self._pending is not None:
            pending_id, _ = self._pending
            self._pending = None
            self._emit(out, error_response(pending_id, CLIENT_DISCONNECTED, reason))
        self._info(out, f"клиент отключён: {reason}")

    # -- команды -------------------------------------------------------------

    def _handle_line(self, line: str, now: float, out: TextIO) -> None:
        text = line.strip()
        if not text:
            return
        try:
            cmd = parse_envelope(text)
        except ProtocolError as exc:
            self._emit(out, error_response(exc.command_id, exc.code, exc.detail))
            return
        client = self._client
        if client is None:
            self._emit(out, error_response(cmd.id, NO_CLIENT, "клиент-расширение не подключён"))
            return
        envelope = serialize(
            {
                "v": PROTOCOL_VERSION,
                "id": cmd.id,
                "action": cmd.action,
                "payload": cmd.payload,
            }
        )
        try:
            client.send_text(envelope)
        except (WSError, OSError) as exc:
            self._emit(out, error_response(cmd.id, CLIENT_DISCONNECTED, str(exc)))
            return
        self.commands_forwarded += 1
        self._pending = (cmd.id, now + self.response_timeout)

    # -- heartbeat и дедлайны --------------------------------------------------

    def _select_timeout(self, now: float) -> float | None:
        deadlines = []
        if self._client is not None:
            deadlines.append(self._next_ping)
            deadlines.append(self._last_seen + self.heartbeat_timeout)
        if self._pending is not None:
            deadlines.append(self._pending[1])
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def _check_deadlines(self, now: float, out: TextIO) -> None:
        if self._pending is not None and now >= self._pending[1]:
            pending_id, _ = self._pending
            self._pending = None
            self._emit(
                out,
                error_response(
                    pending_id,
                    TIMEOUT,
                    f"нет ответа за {self.response_timeout:.0f} с",
                ),
            )
        if self._client is not None:
            if now >= self._next_ping:
                try:
                    self._client.send_ping()
                except (WSError, OSError) as exc:
                    self._drop_client(f"пинг не прошёл ({exc})", out)
                    return
                self._next_ping = now + self.heartbeat_interval
            if now - self._last_seen > self.heartbeat_timeout:
                self._drop_client(
                    f"нет ответов дольше {self.heartbeat_timeout:.0f} с (heartbeat timeout)",
                    out,
                )

    # -- вывод ----------------------------------------------------------------

    def _emit(self, out: TextIO, obj: dict) -> None:
        out.write(serialize(obj) + "\n")
        out.flush()

    def _info(self, out: TextIO, message: str) -> None:
        out.write(f"[INFO] {message}\n")
        out.flush()

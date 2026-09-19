"""live-канал этапа 2 эпика #588 (#1159): loopback WS-сервер + версионированный
allowlist-протокол для расширения hhru-live (этап 1: #929/#930).

Пакет — только транспорт: он принимает команды от локального вызывающего
(stdin CLI-команды live-serve), валидирует их по протоколу и allowlist и
пересылает подключённому расширению по WebSocket. Исполнителем остаётся
браузер пользователя (реальные DOM-клики расширения); канал не делает никаких
HTTP-вызовов к hh.ru и не запускает браузер. Сценарии поверх канала (S3/S4) —
отдельные ишью, здесь их нет.
"""

from .allowlist import ALLOWED_ACTIONS
from .protocol import (
    PROTOCOL_VERSION,
    Command,
    ProtocolError,
    error_response,
    ok_response,
    parse_envelope,
    parse_response,
    serialize,
)
from .server import LiveServeServer

__all__ = [
    "ALLOWED_ACTIONS",
    "PROTOCOL_VERSION",
    "Command",
    "LiveServeServer",
    "ProtocolError",
    "error_response",
    "ok_response",
    "parse_envelope",
    "parse_response",
    "serialize",
]

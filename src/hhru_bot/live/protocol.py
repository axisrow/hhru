"""Версионированный протокол live-канала (#1159, этап 2 эпика #588).

Envelope команды (локальный вызывающий -> сервер -> расширение)::

    {"v": 1, "id": "c1", "action": "list_overlays", "payload": {}}

Ответ расширения, пробрасываемый вызывающему дословно::

    {"id": "c1", "status": "ok", "result": {...}}

Fail-closed: неизвестная версия протокола, неизвестное действие, битый
envelope, битый payload — явные ошибки :class:`ProtocolError` с machine-кодом;
такая команда НИКОГДА не доходит до исполнителя в браузере. Чистая логика без
I/O — проверяется тестами без сокетов.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

# Ровно одна поддерживаемая версия; несоответствие — unsupported_version.
PROTOCOL_VERSION = 1

# Machine-коды транспортных ошибок (result.code в ответе со status="error").
# Коды ошибок самого расширения (action_not_allowed, no_close_control, ...) —
# данные клиента, транспорт их не интерпретирует и не подменяет.
BAD_ENVELOPE = "bad_envelope"
UNSUPPORTED_VERSION = "unsupported_version"
UNKNOWN_ACTION = "unknown_action"
BAD_PAYLOAD = "bad_payload"
NO_CLIENT = "no_client"
TIMEOUT = "timeout"
CLIENT_DISCONNECTED = "client_disconnected"
BAD_RESPONSE = "bad_response"
UNEXPECTED_MESSAGE = "unexpected_message"


@dataclass(frozen=True)
class ProtocolError(Exception):
    """Отказ валидации протокола с machine-кодом для result.code."""

    code: str
    detail: str
    # id из envelope, если его удалось прочитать, — попадает в ответ об ошибке.
    command_id: str | int | None = None

    def __str__(self) -> str:  # pragma: no cover - диагностика, не логика
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class Command:
    """Валидированная команда envelope'а."""

    id: str | int
    action: str
    payload: dict


def parse_envelope(text: str) -> Command:
    """Разобрать и провалидировать envelope команды; отказ — ProtocolError."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(BAD_ENVELOPE, f"строка не JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(BAD_ENVELOPE, "envelope должен быть JSON-объектом")

    command_id = obj.get("id")
    if not _is_valid_id(command_id):
        raise ProtocolError(BAD_ENVELOPE, "поле id обязательно (непустая строка или число)")

    raw_v = obj.get("v")
    if raw_v is None:
        raise ProtocolError(BAD_ENVELOPE, "нет поля v (версия протокола)", command_id)
    # Строго int: bool исключён (True == 1 в Python), float исключён (1.0 == 1)
    # — версия протокола совпадает только по типу И значению.
    if not isinstance(raw_v, int) or isinstance(raw_v, bool) or raw_v != PROTOCOL_VERSION:
        raise ProtocolError(
            UNSUPPORTED_VERSION,
            f"версия протокола {raw_v!r} не поддерживается, ожидается {PROTOCOL_VERSION}",
            command_id,
        )

    action = obj.get("action")
    if not isinstance(action, str) or not action:
        raise ProtocolError(BAD_ENVELOPE, "поле action обязательно (непустая строка)", command_id)

    # Ленивый импорт: allowlist импортирует ProtocolError отсюда (одна
    # сущность — один источник), цикл разрывается на границе вызова.
    from .allowlist import ALLOWED_ACTIONS

    if action not in ALLOWED_ACTIONS:
        raise ProtocolError(
            UNKNOWN_ACTION,
            f"действие {action!r} вне allowlist ({', '.join(sorted(ALLOWED_ACTIONS))})",
            command_id,
        )

    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError(BAD_PAYLOAD, "payload должен быть JSON-объектом", command_id)
    try:
        ALLOWED_ACTIONS[action](payload)
    except ProtocolError as exc:
        # Валидатор не знает id envelope'а, а без него ответ об ошибке (id=null)
        # не коррелируется с командой: вызывающий ждёт timeout вместо
        # немедленного отказа (#1164, интеграционный тест LiveChannel).
        raise ProtocolError(exc.code, exc.detail, command_id) from exc
    return Command(id=cast("str | int", command_id), action=action, payload=payload)


def ok_response(command_id: str | int | None, result: dict) -> dict:
    return {"id": command_id, "status": "ok", "result": result}


def error_response(command_id: str | int | None, code: str, detail: str | None = None) -> dict:
    result: dict = {"code": code}
    if detail:
        result["detail"] = detail
    return {"id": command_id, "status": "error", "result": result}


def parse_response(text: str) -> dict:
    """Валидировать ответ расширения по форме ответа; отказ — ProtocolError.

    Транспорт проверяет только ФОРМУ ({id, status, result}) — содержимое
    result и client-код ошибки пробрасываются вызывающему дословно.
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(BAD_RESPONSE, f"ответ не JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(BAD_RESPONSE, "ответ должен быть JSON-объектом")
    if not _is_valid_id(obj.get("id")):
        raise ProtocolError(BAD_RESPONSE, "нет поля id или оно пустое")
    if obj.get("status") not in ("ok", "error"):
        raise ProtocolError(BAD_RESPONSE, "поле status должно быть 'ok' или 'error'")
    if not isinstance(obj.get("result"), dict):
        raise ProtocolError(BAD_RESPONSE, "поле result обязательно (объект)")
    return obj


def serialize(obj: dict) -> str:
    """Однострочный JSON для фрейма WS и строки stdout."""
    return json.dumps(obj, ensure_ascii=False)


def _is_valid_id(value: object) -> bool:
    if isinstance(value, str):
        return bool(value)
    # bool — подкласс int: True/False как id не допускаем.
    return isinstance(value, int) and not isinstance(value, bool)

"""Allowlist действий live-канала — зеркало этапа 1 (#1159).

Ровно три действия расширения hhru-live (extensions/hhru-live/content.js,
``ACTION_ALLOWLIST`` / background.js ``RELAY_ACTIONS``)::

    list_overlays                  — payload: {}
    dismiss_overlay {id, selector?} — закрыть safe-overlay (id = "overlay-N")
    check_element   {selector}      — found/visible + obstruction-проба

Транспорт НЕ расширяет allowlist молча: новое действие сначала появляется в
расширении, затем здесь. Каждый валидатор строг к форме payload: чужие ключи,
чужие типы, пустые значения — ProtocolError(BAD_PAYLOAD), команда не
пересылается. Семантику (какая политика классификации, что безопасно закрывать)
канал не решает — это остаётся в policy.js/content.js.
"""

from __future__ import annotations

from collections.abc import Callable

from .protocol import BAD_PAYLOAD, ProtocolError


def _reject(payload: dict, detail: str) -> None:
    raise ProtocolError(BAD_PAYLOAD, detail)


def _check_list_overlays(payload: dict) -> None:
    if payload:
        _reject(payload, "list_overlays не принимает параметров")


def _check_dismiss_overlay(payload: dict) -> None:
    _require_fields(payload, required=(("id", str),), optional=(("selector", str),))


def _check_check_element(payload: dict) -> None:
    _require_fields(payload, required=(("selector", str),), optional=())


def _require_fields(
    payload: dict,
    *,
    required: tuple[tuple[str, type], ...],
    optional: tuple[tuple[str, type], ...],
) -> None:
    known = {name for name, _ in required} | {name for name, _ in optional}
    unknown = sorted(set(payload) - known)
    if unknown:
        _reject(payload, f"неизвестные поля payload: {', '.join(unknown)}")
    for name, expected in required:
        value = payload.get(name)
        if not isinstance(value, expected) or isinstance(value, bool) or not value:
            _reject(payload, f"поле {name} обязательно (непустая строка)")
    for name, expected in optional:
        if name in payload and not isinstance(payload[name], expected):
            _reject(payload, f"поле {name} должно быть строкой")


ALLOWED_ACTIONS: dict[str, Callable[[dict], None]] = {
    "list_overlays": _check_list_overlays,
    "dismiss_overlay": _check_dismiss_overlay,
    "check_element": _check_check_element,
}

"""Allowlist действий live-канала — зеркало расширения hhru-live (S1+S2+S4).

Семь действий расширения (extensions/hhru-live/content.js,
``ACTION_ALLOWLIST`` / background.js ``RELAY_ACTIONS``)::

    list_overlays                  — payload: {}
    dismiss_overlay {id, selector?} — закрыть safe-overlay (id = "overlay-N")
    check_element   {selector}      — found/visible + obstruction-проба
    get_page_state                  — URL/title/readyState вкладки
    wait_element {dataQa|label|selector, state, timeoutMs}
    click_element {dataQa|label|selector, waitFor?, allowApply?}
    fill_element  {dataQa|label|selector, text}

Транспорт НЕ расширяет allowlist молча: новое действие сначала появляется в
расширении, затем здесь. Каждый валидатор строг к форме payload: чужие ключи,
чужие типы, пустые значения — ProtocolError(BAD_PAYLOAD), команда не
пересылается. Семантика (какая политика классификации, что безопасно закрывать,
когда разрешён apply-клик) каналу не принадлежит — policy.js/executor.js решают
в браузере; allowApply здесь только честно переносит явную авторизацию сценария.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

from .protocol import BAD_PAYLOAD, ProtocolError

#: Потолок текста fill_element: сопроводительное письмо — порядок килобайта;
#: всё длиннее — ошибка конфигурации, а не текст для формы.
FILL_TEXT_MAX_LEN = 10_000


def _reject(payload: dict, detail: str) -> NoReturn:
    raise ProtocolError(BAD_PAYLOAD, detail)


def _check_list_overlays(payload: dict) -> None:
    if payload:
        _reject(payload, "list_overlays не принимает параметров")


def _check_get_page_state(payload: dict) -> None:
    if payload:
        _reject(payload, "get_page_state не принимает параметров")


def _check_dismiss_overlay(payload: dict) -> None:
    _require_fields(payload, required=(("id", str),), optional=(("selector", str),))


def _check_check_element(payload: dict) -> None:
    _require_fields(payload, required=(("selector", str),), optional=())


def _check_wait_element(payload: dict) -> None:
    _check_target(payload, extra_known=("state", "timeoutMs"))
    state = payload.get("state")
    if state not in ("visible", "hidden"):
        _reject(payload, "поле state должно быть 'visible' или 'hidden'")
    _check_timeout_ms(payload)


def _check_click_element(payload: dict) -> None:
    _check_target(payload, extra_known=("state", "timeoutMs", "waitFor", "allowApply"))
    allow_apply = payload.get("allowApply")
    if allow_apply is not None and not isinstance(allow_apply, bool):
        _reject(payload, "поле allowApply должно быть булевым")
    wait_for = payload.get("waitFor")
    if wait_for is None:
        return
    if not isinstance(wait_for, dict):
        _reject(payload, "поле waitFor должно быть объектом")
    # Условие post-click валидируется как wait_element: цель + state + timeoutMs.
    _check_wait_element(wait_for)


def _check_fill_element(payload: dict) -> None:
    _check_target(payload, extra_known=("text",))
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        _reject(payload, "поле text обязательно (непустая строка)")
    if len(text) > FILL_TEXT_MAX_LEN:
        _reject(payload, f"поле text длиннее {FILL_TEXT_MAX_LEN} символов")


def _check_target(payload: dict, *, extra_known: tuple[str, ...]) -> None:
    """Ровно один режим адресации — контракт executor.resolveTargets (fail-closed:
    оба или ни одного — ошибка, а не догадка о приоритете)."""
    modes = []
    for name in ("dataQa", "label", "selector"):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            modes.append(name)
    if len(modes) != 1:
        _reject(
            payload,
            "ровно одно поле адресации dataQa|label|selector (непустая строка)"
            f", передано: {', '.join(modes) or 'ничего'}",
        )
    unknown = sorted(set(payload) - {"dataQa", "label", "selector", *extra_known})
    if unknown:
        _reject(payload, f"неизвестные поля payload: {', '.join(unknown)}")


def _check_timeout_ms(payload: dict) -> None:
    timeout = payload.get("timeoutMs")
    # Строго int: bool исключён (True == 1), float — не таймаут протокола.
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        _reject(payload, "поле timeoutMs обязательно (целое число > 0)")


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
    "get_page_state": _check_get_page_state,
    "wait_element": _check_wait_element,
    "click_element": _check_click_element,
    "fill_element": _check_fill_element,
}

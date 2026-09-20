"""Allowlist действий live-канала — зеркало этапов 1 и 2 (#1159, #1160).

Ровно шесть действий расширения hhru-live (extensions/hhru-live/content.js,
``ACTION_ALLOWLIST`` / background.js ``RELAY_ACTIONS``)::

    list_overlays                  — payload: {}
    dismiss_overlay {id, selector?} — закрыть safe-overlay (id = "overlay-N")
    check_element   {selector}      — found/visible + obstruction-проба
    get_page_state  {}              — url/title/readyState вкладки
    click_element   {selector|dataQa|label, waitFor} — клик через policy-ядро
    wait_element    {selector|dataQa|label, state, timeoutMs}

Транспорт НЕ расширяет allowlist молча: новое действие сначала появляется в
расширении, затем здесь. Каждый валидатор строг к форме payload: чужие ключи,
чужие типы, пустые значения — ProtocolError(BAD_PAYLOAD), команда не
пересылается. Формы click_element/wait_element повторяют resolver'ы
extensions/hhru-live/executor.js (resolveTargets/resolveWait/resolveTimeout):
ровно один непустой способ адресации, состояние visible|hidden, положительный
числовой timeoutMs (camelCase — имена полей сообщения исполнителя). Семантику
(какая политика классификации, что безопасно закрывать/кликать) канал не
решает — это остаётся в policy.js/content.js.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

from .protocol import BAD_PAYLOAD, ProtocolError

# Способы адресации элемента (executor.js resolveTargets: ровно один).
_ADDRESSING_FIELDS = ("selector", "dataQa", "label")
# Состояния ожидания (executor.js: state_required).
_WAIT_STATES = ("visible", "hidden")


def _reject(payload: dict, detail: str) -> NoReturn:
    raise ProtocolError(BAD_PAYLOAD, detail)


def _reject_unknown(payload: dict, known: tuple[str, ...]) -> None:
    unknown = sorted(set(payload) - set(known))
    if unknown:
        _reject(payload, f"неизвестные поля payload: {', '.join(unknown)}")


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


def _check_one_target(payload: dict, context: str) -> None:
    """Ровно один непустой строковый способ адресации (executor.js: target_required).

    Чужие ключи здесь не проверяются: набор известных полей у каждого
    действия свой, их отвергает валидатор действия до этого вызова.
    """
    filled = [
        name
        for name in _ADDRESSING_FIELDS
        if isinstance(payload.get(name), str) and payload[name].strip()
    ]
    if len(filled) != 1:
        _reject(
            payload,
            f"{context}: ровно один способ адресации "
            f"({', '.join(_ADDRESSING_FIELDS)} — непустой строкой)",
        )


def _check_timeout_ms(payload: dict) -> None:
    value = payload.get("timeoutMs")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        _reject(payload, "timeoutMs обязателен (положительное число миллисекунд)")


def _check_wait_element(payload: dict) -> None:
    _reject_unknown(payload, (*_ADDRESSING_FIELDS, "state", "timeoutMs"))
    _check_one_target(payload, "wait_element")
    if payload.get("state") not in _WAIT_STATES:
        _reject(payload, "state обязателен: 'visible' или 'hidden'")
    _check_timeout_ms(payload)


def _check_wait_for(wait_for: object) -> None:
    """Объявленное пост-клик условие; без него клик не проходит (wait_required)."""
    if not isinstance(wait_for, dict):
        _reject({}, "waitFor должен быть JSON-объектом")
    _reject_unknown(wait_for, (*_ADDRESSING_FIELDS, "state", "timeoutMs"))
    if wait_for.get("state") not in _WAIT_STATES:
        _reject(wait_for, "waitFor.state обязателен: 'visible' или 'hidden'")
    _check_timeout_ms(wait_for)
    _check_one_target(wait_for, "waitFor")


def _check_click_element(payload: dict) -> None:
    _reject_unknown(payload, (*_ADDRESSING_FIELDS, "waitFor"))
    _check_one_target(payload, "click_element")
    if "waitFor" not in payload:
        _reject(
            payload,
            "click_element требует waitFor — клик без объявленного "
            "пост-клик условия отклоняется исполнителем (wait_required)",
        )
    _check_wait_for(payload["waitFor"])


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
    "click_element": _check_click_element,
    "wait_element": _check_wait_element,
}

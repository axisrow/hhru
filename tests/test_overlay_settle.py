"""wait_overlay_settled — wait-механизм гонок монтажа оверлеев (#1231).

Гонка моделируется фейк-ридером: первые чтения отдают пустой census, модалка
появляется в поздних вызовах (relocation-класс #1135: монтаж 1.5-8 с после
клика/чтения). Устойчивость в юнит-контуре доказывается числом чтений и
сжатыми окнами, а не wall-clock: боевые паузы живут в константах модуля.
"""

from __future__ import annotations

import time

import pytest

from hhru_bot.live.overlay_settle import overlay_fingerprint, wait_overlay_settled

pytestmark = pytest.mark.unit

GEO = {
    "id": "ov-1",
    "type": "modal",
    "disposition": "ambiguous",
    "closeControls": 0,
    "text": "Ваш регион — Москва? Да, верно Нет, другой",
}


class LateReader:
    """Ридер с поздним монтажом: первые silent_calls чтений — пусто, дальше — overlay."""

    def __init__(self, overlay: dict, silent_calls: int = 2) -> None:
        self.overlay = overlay
        self.silent_calls = silent_calls
        self.calls = 0

    def __call__(self) -> list[dict]:
        self.calls += 1
        if self.calls <= self.silent_calls:
            return []
        return [dict(self.overlay)]


class ChurningReader:
    """Набор никогда не стабилизируется: id меняется на каждом чтении."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> list[dict]:
        self.calls += 1
        return [
            {"id": f"ov-{self.calls}", "type": "modal", "disposition": "safe", "closeControls": 1}
        ]


class BrokenReader:
    """Канал без list_overlays (старые channel-like фейки команд)."""

    def __call__(self) -> list[dict]:
        raise AttributeError("'FakeChannel' object has no attribute 'list_overlays'")


def test_settles_after_stable_window() -> None:
    reader = LateReader(GEO, silent_calls=1000)  # census пуст всегда
    start = time.monotonic()
    snapshot = wait_overlay_settled(reader, timeout_ms=5_000, stable_ms=120, poll_ms=20)
    elapsed = time.monotonic() - start

    assert snapshot == []
    # Возврат после окна стабильности, а не полного таймаута — цена
    # счастливого пути (чистая вкладка) это stable_ms, не timeout_ms.
    assert 0.1 <= elapsed < 1.0
    assert reader.calls >= 2


def test_sees_late_mount() -> None:
    # ГОНКА (#1231): модалка смонтировалась после первых чтений — хелпер
    # возвращает снимок С ней, а не домонтировочный пустой.
    reader = LateReader(GEO, silent_calls=2)
    snapshot = wait_overlay_settled(reader, timeout_ms=5_000, stable_ms=80, poll_ms=20)

    assert snapshot is not None
    assert [overlay["id"] for overlay in snapshot] == ["ov-1"]
    assert reader.calls > 2


def test_timeout_unstable_returns_none() -> None:
    # Набор не устаканился за бюджет — None (fail-closed: потребитель ведёт
    # себя как при недоступном census, вердикт прежний).
    reader = ChurningReader()
    snapshot = wait_overlay_settled(reader, timeout_ms=200, stable_ms=50, poll_ms=20)

    assert snapshot is None
    assert reader.calls >= 2


def test_reader_errors_return_none() -> None:
    # Широкий лов как в _overlays_best_effort: AttributeError/канальный сбой
    # чтения — None, а не исключение из сценария.
    assert wait_overlay_settled(BrokenReader(), timeout_ms=200, stable_ms=50, poll_ms=20) is None


def test_fingerprint_ignores_text_and_order() -> None:
    a = [{"id": "ov-1", "type": "modal", "disposition": "safe", "closeControls": 2, "text": "А"}]
    b = [{"id": "ov-1", "type": "modal", "disposition": "safe", "closeControls": 2, "text": "Б"}]
    # Текст не участвует: census может отдавать его лениво — смена текста
    # при том же наборе узлов не событие монтажа/исчезновения.
    assert overlay_fingerprint(a) == overlay_fingerprint(b)
    assert overlay_fingerprint(a) != overlay_fingerprint([dict(a[0], id="ov-2")])
    # Порядок узлов census не документирован — отпечаток от него не зависит.
    pair = [dict(a[0], id="ov-2"), dict(a[0], id="ov-3")]
    assert overlay_fingerprint(pair) == overlay_fingerprint(list(reversed(pair)))

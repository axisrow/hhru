"""Wait-механизм гонок монтажа/исчезновения оверлеев (#1231).

Модалки hh.ru монтируются в окне 1.5-8 с после клика/загрузки (relocation-
класс, #1132/#1135/#1214), поэтому одиночный снимок census может быть
домонтировочным: вердикт по нему — вердикт по устаревшему DOM. Хелпер читает
census поллом до стабилизации набора и отдаёт устойчивый снимок вердиктным
точкам сценария. Только stdlib: unit-тесты гоняют его с фейк-ридером на
голом main, без канала и без playwright.

Границы (#1231):

- это НЕ сериализация конкурентных чтений — канал однопоточен по протоколу
  (``LiveChannel._call`` держит одну команду в полёте), хелпер stateless;
- «устаканился» не равно «больше не появится»: монтаж позже ``timeout_ms``
  даёт None и прежнее поведение потребителя — fail-closed сохранён;
- замена дублей боевого пути (``STALE_ALERT_RENDER_WAIT_MS`` и т.п.) живёт
  в самих модулях: Playwright-ожидание не имеет дешёвого census-полла,
  боевые значения таймаутов не менялись (#1231, раздел «законно разные»).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

# Верх документированного окна монтажа модалок (1.5-8 с, #1135): после него
# набор считается неустойчивым, потребитель ведёт себя как при недоступном
# census (None — прежнее поведение, не новый отказ).
OVERLAY_SETTLE_TIMEOUT_MS = 8_000
# Набор оверлеев, не менявшийся это окно, считается устаканившимся; оно же —
# цена счастливого пути (чистая вкладка) одного вызова.
OVERLAY_SETTLE_STABLE_MS = 1_000
# Шаг полла: чтение list_overlays — round-trip до вкладки, чаще незачем.
OVERLAY_SETTLE_POLL_MS = 250


def overlay_fingerprint(overlays: Iterable[dict]) -> tuple:
    """Стабильный отпечаток набора оверлеев для сравнения снимков.

    Текст не участвует: content.js может отдавать его лениво/обрезанно —
    смена текста при том же узле не событие монтажа/исчезновения. Кортеж
    сортирован: порядок узлов census не документирован. closeControls
    сворачивается в факт наличия — гейт dismiss'а (#1218) использует ровно
    его; не-dict записи (битый payload расширения) игнорируются, как в
    `_overlays_best_effort`.
    """
    return tuple(
        sorted(
            (
                str(overlay.get("id")),
                str(overlay.get("type")),
                str(overlay.get("disposition")),
                bool(overlay.get("closeControls")),
            )
            for overlay in overlays
            if isinstance(overlay, dict)
        )
    )


def wait_overlay_settled(
    read_overlays: Callable[[], list[dict]],
    *,
    timeout_ms: int | None = None,
    stable_ms: int | None = None,
    poll_ms: int | None = None,
) -> list[dict] | None:
    """Полл read_overlays до стабилизации набора оверлеев (#1231).

    Цикл «снимок -> отпечаток»: отпечаток, не менявшийся ``stable_ms``,
    считается устаканившимся — возвращается последний снимок. Оверлей,
    смонтировавшийся посреди окна, меняет отпечаток и полл продолжается,
    поэтому вердиктная точка видит поздний монтаж, а не устаревший снимок.

    None — таймаут без стабилизации или сбой чтения: широкий лов повторяет
    контракт `_overlays_best_effort` (AttributeError у channel-like фейков
    без list_overlays, канальные ошибки) — потребитель ведёт себя как
    сегодня при недоступном census, прежние тесты живут без правок. None не
    отличим от «пусто» — потребитель обязан учитывать оба.

    Аргументы-None читаются из тела (не дефолты сигнатуры): тесты правят
    константы патчем модуля одним местом. Канал однопоточен по протоколу,
    лок не заводится (граница #1231).
    """
    timeout = OVERLAY_SETTLE_TIMEOUT_MS if timeout_ms is None else timeout_ms
    stable = OVERLAY_SETTLE_STABLE_MS if stable_ms is None else stable_ms
    poll = OVERLAY_SETTLE_POLL_MS if poll_ms is None else poll_ms
    deadline = time.monotonic() + timeout / 1000
    stable_s = stable / 1000
    last_fingerprint = None
    last_change = time.monotonic()
    while True:
        try:
            snapshot = [overlay for overlay in read_overlays() or [] if isinstance(overlay, dict)]
        except Exception:  # noqa: BLE001 — census best-effort, вердикт прежний
            return None
        fingerprint = overlay_fingerprint(snapshot)
        now = time.monotonic()
        if fingerprint != last_fingerprint:
            last_fingerprint = fingerprint
            last_change = now
        elif now - last_change >= stable_s:
            return snapshot
        if now >= deadline:
            return None
        time.sleep(min(poll / 1000, max(0.0, deadline - now)))

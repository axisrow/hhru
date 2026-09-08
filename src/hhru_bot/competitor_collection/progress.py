"""Вывод прогресса и человекочитаемые оценки сборщика конкурентов (#1047).

`progress()` — единственный канал сообщений хода сбора: stdout (гасится при
`quiet` и переживает оборванный PTY) плюс дублирование в файловый лог.
Чистые форматтеры (`format_*`, `observed_eta`, `throttle_estimate`) отделены
от побочных эффектов и тестируются напрямую.
"""

from __future__ import annotations

import logging
import math

from .state import RunSnapshot

logger = logging.getLogger("hhru_bot.competitors")


def progress(
    message: str,
    *,
    quiet: bool,
    level: int = logging.INFO,
    always: bool = False,
) -> None:
    if always or not quiet:
        try:
            print(message, flush=True)
        except BrokenPipeError:
            # A detached PTY must not kill collection before the durable
            # checkpoint/finalizer can run. The file log remains available.
            pass
    record = logger.makeRecord(logger.name, level, __file__, 0, message, (), None)
    for handler in logging.getLogger("hhru_bot").handlers:
        if isinstance(handler, logging.FileHandler):
            handler.handle(record)


def format_duration(seconds: float) -> str:
    if seconds < 30:
        return f"{max(0, round(seconds))} с"
    total_minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    parts.append(f"{seconds} с")
    return " ".join(parts)


def observed_eta(snapshot: RunSnapshot, *, elapsed: float) -> str | None:
    processed = snapshot.saved + snapshot.failed
    expected = snapshot.expected_details
    if not expected or processed < 3 or processed >= expected or elapsed <= 0:
        return None
    seconds = elapsed / processed * (expected - processed)
    return (
        f"осталось~{format_duration(seconds)} "
        f"(диапазон {format_duration(seconds * 0.75)}-{format_duration(seconds * 1.25)})"
    )


def throttle_estimate(
    *,
    details: int,
    requested_page_size: int,
    observed_page_size: int,
    min_delay: float,
    max_delay: float,
    workers: int = 1,
) -> str:
    active_workers = max(1, min(workers, details))
    # Every request — the first included — now waits the configured delay
    # (competitor_workers._worker_main, #663 Codex review), so a worker's
    # wait count equals its request count, not request count minus one.
    waits = math.ceil(details / active_workers)
    return (
        f"запрошено={requested_page_size}/стр., фактически={observed_page_size}/стр., "
        f"объём~{details} деталей, workers={active_workers}; только паузы троттлинга "
        f"{format_duration(waits * min_delay)}-{format_duration(waits * max_delay)}; "
        "ETA уточнится по фактической скорости"
    )

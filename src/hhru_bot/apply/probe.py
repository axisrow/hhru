"""Хук диагностического снимка (probe) состояния страницы.

Владелец: #8. В Wave 0 хук нейтральный — no-op. pipeline.py вызывает
ctx.probe(stage, ...) в стратегических точках; пока это ничего не делает.
#8 подменит ProbeHook на реальный dump_probe_snapshot (HTML/screenshot/DOM),
не трогая pipeline.py — последовательность шагов и точки вызова фиксированы здесь.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("hhru_bot.apply.probe")


class ProbeHook:
    """Нейтральный хук (no-op) по умолчанию. #8 подменяет поведение."""

    def __call__(self, stage: str, **kwargs: Any) -> None:
        logger.debug("probe[%s] (no-op): %s", stage, ", ".join(kwargs))


# Синглтон-заглушка для Wave 0.
NOOP_PROBE = ProbeHook()

"""Жизненный цикл сборщика конкурентов, отделённый от CLI (#1047)."""

from .lifecycle import (
    CollectionLifecycle,
    Heartbeat,
    SignalTermination,
    install_termination_handlers,
    restore_signal_handlers,
)
from .progress import format_duration, format_elapsed, observed_eta, progress, throttle_estimate
from .service import CollectionOutcome, CollectionParams, collect_pages
from .state import (
    CollectionRunState,
    RunSnapshot,
    collection_status,
    page_cap_reached,
)

__all__ = [
    "CollectionLifecycle",
    "CollectionOutcome",
    "CollectionParams",
    "CollectionRunState",
    "Heartbeat",
    "RunSnapshot",
    "SignalTermination",
    "collect_pages",
    "collection_status",
    "format_duration",
    "format_elapsed",
    "install_termination_handlers",
    "observed_eta",
    "page_cap_reached",
    "progress",
    "restore_signal_handlers",
    "throttle_estimate",
]

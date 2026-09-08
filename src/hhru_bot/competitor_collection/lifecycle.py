"""Жизненный цикл collection run: checkpoint, финализация, сигналы (#1047).

Адаптер `CollectionLifecycle` — единственное место, где снапшот состояния
попадает в SQLite (heartbeat/checkpoint и finish). `SignalTermination` и
`Heartbeat` — собственная механика коллектора; общую инфраструктуру сигналов
с supervision (#459) здесь сознательно не трогаем — контракты различаются
и не зафиксированы.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable

from ..history import History
from .progress import observed_eta, progress
from .state import RunSnapshot


class SignalTermination(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def install_termination_handlers() -> dict[int, object]:
    handled_signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in handled_signals}

    def terminate(signum, _frame):  # noqa: ANN001
        raise SignalTermination(signum)

    for signum in handled_signals:
        signal.signal(signum, terminate)
    return previous


def restore_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)  # type: ignore[arg-type]


class CollectionLifecycle:
    """Адаптер history-хранения: checkpoint и финализация одного run."""

    def __init__(self, history: History, run_id: str) -> None:
        self.run_id = run_id
        self.history = history
        # Snapshot and SQLite write are one ordered operation. Without this
        # lock a delayed heartbeat could overwrite a newer completed-page
        # checkpoint written by the main thread (#654 Codex review).
        self._checkpoint_lock = threading.Lock()

    def checkpoint(self, snapshot: RunSnapshot) -> None:
        with self._checkpoint_lock:
            self.history.checkpoint_competitor_collection(
                self.run_id,
                pages_fetched=snapshot.pages,
                cards_seen=snapshot.cards,
                details_saved=snapshot.saved,
                details_failed=snapshot.failed,
                last_started_page=snapshot.last_started_page,
                last_completed_page=snapshot.last_completed_page,
                resume_page=snapshot.resume_page,
                observed_page_size=snapshot.observed_page_size,
                cards_seen_completed=snapshot.cards_completed,
            )

    def finish(
        self,
        snapshot: RunSnapshot,
        *,
        status: str,
        detail: str | None,
        exit_code: int,
        resume_page: int | None,
    ) -> None:
        self.history.finish_competitor_collection(
            self.run_id,
            status=status,
            pages_fetched=snapshot.pages,
            cards_seen=snapshot.cards,
            details_saved=snapshot.saved,
            details_failed=snapshot.failed,
            detail=detail,
            exit_code=exit_code,
            resume_page=resume_page,
            last_started_page=snapshot.last_started_page,
            last_completed_page=snapshot.last_completed_page,
            observed_page_size=snapshot.observed_page_size,
            cards_seen_completed=snapshot.cards_completed,
        )


class Heartbeat:
    def __init__(
        self,
        snapshot: Callable[[], RunSnapshot],
        checkpoint: Callable[[], None],
        *,
        run_id: str,
        quiet: bool,
        started_at: float,
    ):
        self.snapshot = snapshot
        self.checkpoint = checkpoint
        self.run_id = run_id
        self.quiet = quiet
        self.started_at = started_at
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.failure: Exception | None = None

    def _run(self) -> None:
        while not self.stop.wait(45):
            try:
                self.checkpoint()
            except Exception as exc:
                self.failure = exc
                progress(
                    f"[FAIL] run_id={self.run_id} heartbeat не сохранён: "
                    f"{type(exc).__name__}: {exc}",
                    quiet=self.quiet,
                    level=logging.ERROR,
                    always=True,
                )
                return
            snap = self.snapshot()
            page = snap.last_started_page
            page_label = page + 1 if page is not None else "не начата"
            eta = observed_eta(snap, elapsed=time.monotonic() - self.started_at)
            eta_suffix = f", {eta}" if eta else ""
            progress(
                f"[HEARTBEAT] run_id={self.run_id} competitors collect жив: "
                f"страница={page_label}, карточек={snap.cards}, "
                f"сохранено={snap.saved}, ошибок={snap.failed}{eta_suffix}",
                quiet=self.quiet,
            )

    def raise_if_failed(self) -> None:
        if self.failure is not None:
            raise self.failure

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        self.thread.join(timeout=1)

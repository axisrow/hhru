"""Process-owned Playwright workers for competitor resume details.

The sync Playwright API is not thread-safe.  Each worker therefore owns its
own Python process, Playwright driver, browser context, and page.  The parent
process remains the sole SQLite/checkpoint writer.
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import random
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DetailWorkerConfig:
    storage_state_file: str | None
    headless: bool
    user_agent: str | None
    min_delay_seconds: float
    max_delay_seconds: float
    require_authentication: bool


def _worker_main(
    worker_id: int,
    tasks,
    results,
    stop_event,
    config: DetailWorkerConfig,
) -> None:
    # Keep terminal Ctrl-C scoped to the durable parent. Worker-owned
    # Playwright drivers then stay alive long enough for an orderly close
    # instead of dumping Node EPIPE traces into stdout.
    if hasattr(os, "setsid"):
        os.setsid()
    # Ctrl-C belongs to the durable parent. It coordinates checkpointing and
    # terminates workers after it has stopped dispatching new cards.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        from .apply.antibot import AntiBotChallengeDetected
        from .browser import launch_context
        from .competitors import CompetitorResumeIndeterminate, fetch_competitor_resume

        storage_state = Path(config.storage_state_file) if config.storage_state_file else None
        with launch_context(
            storage_state,
            headless=config.headless,
            user_agent=config.user_agent,
        ) as context:
            page = context.new_page()
            attempts = 0
            while not stop_event.is_set():
                item = tasks.get()
                if item is None:
                    return
                task_id, card = item
                # Every request — including each worker's first — waits the
                # configured random delay. Skipping it only before "attempts
                # == 0" let every worker fire its first request in the same
                # instant: N workers starting together burst N simultaneous
                # requests at hh.ru with zero delay, defeating the two-level
                # throttle this project relies on to avoid looking like
                # automation (CLAUDE.md "Двухуровневый троттлинг", #663 Codex
                # review). A random stagger before every request — first
                # included — keeps concurrent workers spread out instead of
                # bursting in lockstep.
                delay = random.uniform(
                    config.min_delay_seconds,
                    config.max_delay_seconds,
                )
                if stop_event.wait(delay):
                    return
                attempts += 1
                try:
                    snapshot = fetch_competitor_resume(
                        page,
                        card,
                        require_authentication=config.require_authentication,
                    )
                except AntiBotChallengeDetected as exc:
                    stop_event.set()
                    results.put(
                        {
                            "kind": "antibot",
                            "worker_id": worker_id,
                            "task_id": task_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "antibot_signal": exc.detection.signal,
                            "antibot_detail": exc.detection.detail,
                        }
                    )
                    return
                except (CompetitorResumeIndeterminate, ValueError) as exc:
                    results.put(
                        {
                            "kind": "error",
                            "worker_id": worker_id,
                            "task_id": task_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                except Exception as exc:
                    # Playwright navigation/render failures are per-card. A
                    # dead process is detected separately by the parent.
                    results.put(
                        {
                            "kind": "error",
                            "worker_id": worker_id,
                            "task_id": task_id,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                else:
                    payload = asdict(snapshot)
                    payload["content_hash"] = snapshot.content_hash()
                    results.put(
                        {
                            "kind": "success",
                            "worker_id": worker_id,
                            "task_id": task_id,
                            "payload": payload,
                        }
                    )
    except BaseException as exc:
        results.put(
            {
                "kind": "fatal",
                "worker_id": worker_id,
                "task_id": None,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )


class DetailWorkerPool:
    """Small persistent process pool with explicit lifecycle and health checks."""

    def __init__(self, workers: int, config: DetailWorkerConfig):
        self.workers = workers
        self.config = config
        self._context = multiprocessing.get_context("spawn")
        self._tasks = self._context.Queue()
        self._results = self._context.Queue()
        self._stop_event = self._context.Event()
        self._processes: list[Any] = []
        self._closed = False

    @property
    def size(self) -> int:
        """Number of worker processes started so far."""
        return len(self._processes)

    def start(self) -> None:
        if self._processes:
            return
        self.grow(self.workers)

    def grow(self, target_workers: int) -> None:
        """Start additional worker processes up to ``target_workers`` total.

        The caller may learn the real workload only after the first page of
        cards (mostly-duplicate first pages during a ``--resume`` undersize
        the pool otherwise, #663 Codex review). Growing is additive and
        idempotent — calling with a lower or equal ``target_workers`` is a
        no-op, and existing workers/in-flight tasks are untouched.
        """
        if self._closed:
            raise RuntimeError("detail worker pool уже закрыт")
        start_id = len(self._processes)
        for worker_id in range(start_id, target_workers):
            process = self._context.Process(
                target=_worker_main,
                args=(
                    worker_id,
                    self._tasks,
                    self._results,
                    self._stop_event,
                    self.config,
                ),
                name=f"hhru-competitor-detail-{worker_id + 1}",
            )
            process.start()
            self._processes.append(process)

    def submit(self, task_id: int, card: Any) -> None:
        if self._closed:
            raise RuntimeError("detail worker pool уже закрыт")
        self._tasks.put((task_id, card))

    def result(self, *, timeout: float = 1.0) -> dict | None:
        try:
            return self._results.get(timeout=timeout)
        except queue.Empty:
            dead = [process for process in self._processes if not process.is_alive()]
            if dead:
                labels = ", ".join(f"{process.name}:exit={process.exitcode}" for process in dead)
                raise RuntimeError(f"detail worker завершился без результата: {labels}") from None
            return None

    def close(self, *, terminate: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if not terminate:
            for _ in self._processes:
                self._tasks.put(None)
            deadline = time.monotonic() + 3
            for process in self._processes:
                process.join(timeout=max(0, deadline - time.monotonic()))
        for process in self._processes:
            if process.is_alive():
                process.terminate()
        for process in self._processes:
            process.join(timeout=2)
        self._tasks.close()
        self._results.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.close(terminate=exc_type is not None)

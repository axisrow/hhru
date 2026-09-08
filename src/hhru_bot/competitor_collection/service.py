"""Сервис сбора: цикл страниц и обработчик одной страницы (#1047).

`collect_pages` — это бывший цикл `run_collect`, очищенный от CLI: он не
парсит аргументы и не печатает финальный отчёт, а получает параметры и канал
прогресса и всегда возвращает `CollectionOutcome` — в том числе при
прерывании сигналом или падении браузера (финализация run в SQLite гарантирована
внутри, до возврата). `DetailWorkerPool` используется как раньше; поведение
частичных фейлов, rank offset и scope checkpoint не менялись.

Браузерные и page-парсеры импортируются лениво ВНУТРИ `collect_pages` —
так же, как это делал `run_collect`: тесты монкипатчат атрибуты модулей
`hhru_bot.browser`/`hhru_bot.competitors`/`hhru_bot.competitor_workers`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..exit_codes import CommandExitCode
from .lifecycle import (
    CollectionLifecycle,
    Heartbeat,
    SignalTermination,
    install_termination_handlers,
    restore_signal_handlers,
)
from .progress import observed_eta, progress, throttle_estimate
from .state import CollectionRunState, RunSnapshot, collection_status, page_cap_reached

logger = logging.getLogger("hhru_bot.competitors")

# Канал прогресса CLI: print+файл-лог с заранее подшитым quiet.
Emit = Callable[..., None]


@dataclass(frozen=True)
class CollectionParams:
    """Всё, что сервису нужно от CLI, кроме history-адаптера и прогресса."""

    query: str
    search_in: str
    auth_mode: str
    require_authentication: bool
    requested_page_size: int
    max_pages: int | None
    detail_workers: int
    headless: bool
    quiet: bool
    user_agent: str | None
    launch_storage_state: Path | None
    worker_storage_state: str | None
    min_delay_seconds: float
    max_delay_seconds: float
    initial_page: int
    rank_offset_base: int
    started_at: float


@dataclass(frozen=True)
class CollectionOutcome:
    snapshot: RunSnapshot
    new: int
    updated: int
    unchanged: int
    details_failed: int
    limited: bool
    coverage: object | None
    status: str
    exit_code: int
    detail: str | None
    caught: BaseException | None
    elapsed_seconds: float


def collect_pages(
    params: CollectionParams,
    state: CollectionRunState,
    lifecycle: CollectionLifecycle,
    *,
    emit: Emit = progress,
) -> CollectionOutcome:
    from ..browser import goto_hh, launch_context
    from ..competitor_workers import DetailWorkerConfig, DetailWorkerPool
    from ..competitors import (
        build_competitor_search_url,
        has_next_search_page,
        inspect_search_coverage,
        parse_search_page,
    )

    run_id = lifecycle.run_id
    quiet = params.quiet
    new = updated = unchanged = 0
    limited = False
    detail_attempts = 0
    coverage = None
    target_pages: int | None = None
    seen_resume_ids: set[str] = set()
    pages_this_run = 0
    page_num = params.initial_page
    rank_offset_base = params.rank_offset_base

    def checkpoint() -> None:
        lifecycle.checkpoint(state.snapshot())

    previous_handlers = install_termination_handlers()

    caught: BaseException | None = None
    heartbeat: Heartbeat | None = None
    try:
        with (
            Heartbeat(
                state.snapshot,
                checkpoint,
                run_id=run_id,
                quiet=quiet,
                started_at=params.started_at,
            ) as heartbeat,
            launch_context(
                params.launch_storage_state,
                headless=params.headless,
                user_agent=params.user_agent,
            ) as context,
        ):
            search_page = context.new_page()
            worker_pool = None
            try:
                while True:
                    state.begin_page(page_num)
                    checkpoint()
                    goto_hh(
                        search_page,
                        build_competitor_search_url(
                            params.query,
                            page_num,
                            items_per_page=params.requested_page_size,
                            search_in=params.search_in,
                        ),
                    )
                    cards_before = state.snapshot().cards
                    cards = parse_search_page(
                        search_page,
                        rank_offset=rank_offset_base + cards_before,
                        expected_page_size=params.requested_page_size,
                        require_authentication=params.require_authentication,
                    )
                    pages_this_run += 1
                    state.add_page_results(len(cards))
                    has_next = has_next_search_page(search_page, page_num)
                    if coverage is None:
                        observed_page_size = state.snapshot().observed_page_size
                        coverage = inspect_search_coverage(
                            search_page,
                            page_num,
                            observed_page_size=observed_page_size,
                            requested_page_size=params.requested_page_size,
                        )
                        available_from_here = (
                            max(1, coverage.available_pages - page_num)
                            if coverage.available_pages is not None
                            else None
                        )
                        target_pages = params.max_pages
                        if target_pages is None:
                            target_pages = available_from_here
                        elif available_from_here is not None:
                            target_pages = min(target_pages, available_from_here)
                        if target_pages is not None and observed_page_size:
                            expected_details = (
                                len(cards) + max(0, target_pages - 1) * params.requested_page_size
                            )
                            emit(
                                f"[ESTIMATE] run_id={run_id} "
                                + throttle_estimate(
                                    details=expected_details,
                                    requested_page_size=params.requested_page_size,
                                    observed_page_size=observed_page_size,
                                    min_delay=params.min_delay_seconds,
                                    max_delay=params.max_delay_seconds,
                                    workers=params.detail_workers,
                                ),
                            )
                    if target_pages is not None:
                        state.set_expected_details_from_target(
                            target_pages, pages_this_run, params.requested_page_size
                        )

                    page_cards = []
                    for card in cards:
                        assert heartbeat is not None
                        heartbeat.raise_if_failed()
                        if card.resume_id in seen_resume_ids:
                            continue
                        seen_resume_ids.add(card.resume_id)
                        page_cards.append(card)

                    if page_cards:
                        # Sizing from just this page (instead of capping at
                        # detail_workers outright) undersizes the pool
                        # for the rest of the run when an early page is
                        # mostly duplicates (e.g. --resume) — #663 review.
                        # grow() is additive/idempotent, so re-evaluating the
                        # target on every page lets the pool catch up once a
                        # later page proves there is more work than workers.
                        target_workers = min(params.detail_workers, state.snapshot().cards)
                        if worker_pool is None:
                            worker_pool = DetailWorkerPool(
                                target_workers,
                                DetailWorkerConfig(
                                    storage_state_file=params.worker_storage_state,
                                    headless=params.headless,
                                    user_agent=params.user_agent,
                                    min_delay_seconds=params.min_delay_seconds,
                                    max_delay_seconds=params.max_delay_seconds,
                                    require_authentication=params.require_authentication,
                                ),
                            )
                            worker_pool.start()
                        elif target_workers > worker_pool.size:
                            worker_pool.grow(target_workers)
                        emit(
                            f"[WORKERS] run_id={run_id} запущено={worker_pool.size}",
                        )

                    pending: dict[int, object] = {}
                    if worker_pool is not None:
                        for card in page_cards:
                            task_id = detail_attempts
                            detail_attempts += 1
                            pending[task_id] = card
                            worker_pool.submit(task_id, card)

                    while pending:
                        assert heartbeat is not None
                        heartbeat.raise_if_failed()
                        assert worker_pool is not None
                        result = worker_pool.result(timeout=1)
                        if result is None:
                            continue
                        kind = result["kind"]
                        if kind == "fatal":
                            raise RuntimeError(
                                f"detail worker {result['worker_id'] + 1}: "
                                f"{result['error_type']}: {result['error']}"
                            )
                        task_id = result["task_id"]
                        if task_id not in pending:
                            continue
                        card = pending.pop(task_id)
                        if kind == "antibot":
                            from ..apply.antibot import (
                                AntiBotChallengeDetected,
                                AntiBotDetection,
                            )

                            raise AntiBotChallengeDetected(
                                AntiBotDetection(
                                    signal=result["antibot_signal"],
                                    detail=result["antibot_detail"],
                                )
                            )
                        if kind == "error":
                            state.mark_failed()
                            emit(
                                f"[WARN] run_id={run_id} резюме rank={card.rank} "
                                f"не сохранено: {result['error_type']}: {result['error']}",
                                level=logging.WARNING,
                            )
                            continue
                        outcome = lifecycle.history.upsert_competitor_resume(
                            result["payload"],
                            search_query=params.query,
                            search_rank=card.rank,
                            search_in=params.search_in,
                            auth_mode=params.auth_mode,
                        )
                        state.mark_saved()
                        if outcome == "new":
                            new += 1
                        elif outcome == "updated":
                            updated += 1
                        else:
                            unchanged += 1

                    state.complete_page(
                        page_num,
                        has_next=has_next,
                        cap_reached=page_cap_reached(params.max_pages, pages_this_run, has_next),
                    )
                    checkpoint()
                    current = state.snapshot()
                    eta = observed_eta(current, elapsed=time.monotonic() - params.started_at)
                    eta_suffix = f", {eta}" if eta else ""
                    emit(
                        f"[PROGRESS] run_id={run_id} страница={page_num + 1}, "
                        f"карточек={current.cards}, "
                        f"деталей={current.saved + current.failed}, "
                        f"новых/обновлено={new + updated}, ошибок={current.failed}"
                        f"{eta_suffix}",
                    )
                    if not has_next:
                        break
                    if page_cap_reached(params.max_pages, pages_this_run, has_next):
                        limited = True
                        break
                    page_num += 1
            except BaseException as exc:
                if worker_pool is not None:
                    worker_pool.close(
                        terminate=not isinstance(exc, (KeyboardInterrupt, SignalTermination))
                    )
                raise
            else:
                if worker_pool is not None:
                    worker_pool.close()
    except BaseException as exc:
        caught = exc
    finally:
        restore_signal_handlers(previous_handlers)

    current = state.snapshot()
    elapsed_seconds = time.monotonic() - params.started_at
    if caught is not None:
        status = (
            "partial"
            if current.pages
            or current.cards
            or current.saved
            or current.failed
            or current.last_started_page is not None
            else "failed"
        )
        if isinstance(caught, KeyboardInterrupt):
            code = CommandExitCode.SIGINT.value
        elif isinstance(caught, SignalTermination):
            code = 128 + caught.signum
        else:
            code = 1
        detail = f"{type(caught).__name__}: {caught}"[:1000]
        lifecycle.finish(
            current,
            status=status,
            detail=detail,
            exit_code=code,
            resume_page=current.resume_page,
        )
        return CollectionOutcome(
            snapshot=current,
            new=new,
            updated=updated,
            unchanged=unchanged,
            details_failed=current.failed,
            limited=False,
            coverage=coverage,
            status=status,
            exit_code=code,
            detail=detail,
            caught=caught,
            elapsed_seconds=elapsed_seconds,
        )

    details_failed = current.failed
    status = collection_status(details_failed=details_failed, limited=limited)
    finish_detail = f"limited_by_max_pages={params.max_pages}" if limited else None
    exit_code = 1 if details_failed else 0
    lifecycle.finish(
        current,
        status=status,
        detail=finish_detail,
        exit_code=exit_code,
        resume_page=current.resume_page if limited else None,
    )
    return CollectionOutcome(
        snapshot=current,
        new=new,
        updated=updated,
        unchanged=unchanged,
        details_failed=details_failed,
        limited=limited,
        coverage=coverage,
        status=status,
        exit_code=exit_code,
        detail=finish_detail,
        caught=None,
        elapsed_seconds=elapsed_seconds,
    )

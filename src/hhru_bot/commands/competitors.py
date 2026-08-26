"""Collect and report applicant-visible competitor resumes (#578)."""

from __future__ import annotations

import argparse
import logging
import math
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from ..exit_codes import CommandExitCode

logger = logging.getLogger("hhru_bot.competitors")


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть >= 1")
    return parsed


def _page_size(value: str) -> int:
    parsed = _positive(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("значение должно быть <= 100")
    return parsed


def _detail_workers(value: str) -> int:
    parsed = _positive(value)
    if parsed > 1000:
        raise argparse.ArgumentTypeError("значение должно быть <= 1000")
    return parsed


def _page_cap_reached(max_pages: int | None, pages_fetched: int, has_next: bool) -> bool:
    return has_next and max_pages is not None and pages_fetched >= max_pages


def _collection_status(*, details_failed: int, limited: bool) -> str:
    if limited:
        return "limited"
    return "partial" if details_failed else "complete"


def _format_duration(seconds: float) -> str:
    if seconds < 30:
        return f"{max(0, round(seconds))} с"
    total_minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def _format_elapsed(seconds: float) -> str:
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


def _observed_eta(state: dict, *, elapsed: float) -> str | None:
    processed = state["saved"] + state["failed"]
    expected = state.get("expected_details")
    if not expected or processed < 3 or processed >= expected or elapsed <= 0:
        return None
    seconds = elapsed / processed * (expected - processed)
    return (
        f"осталось~{_format_duration(seconds)} "
        f"(диапазон {_format_duration(seconds * 0.75)}-{_format_duration(seconds * 1.25)})"
    )


def _throttle_estimate(
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
        f"{_format_duration(waits * min_delay)}-{_format_duration(waits * max_delay)}; "
        "ETA уточнится по фактической скорости"
    )


def _progress(
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


class _SignalTermination(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


class _Heartbeat:
    def __init__(
        self,
        snapshot: Callable[[], dict],
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
                _progress(
                    f"[FAIL] run_id={self.run_id} heartbeat не сохранён: "
                    f"{type(exc).__name__}: {exc}",
                    quiet=self.quiet,
                    level=logging.ERROR,
                    always=True,
                )
                return
            state = self.snapshot()
            page = state["last_started_page"]
            page_label = page + 1 if page is not None else "не начата"
            eta = _observed_eta(state, elapsed=time.monotonic() - self.started_at)
            eta_suffix = f", {eta}" if eta else ""
            _progress(
                f"[HEARTBEAT] run_id={self.run_id} competitors collect жив: "
                f"страница={page_label}, карточек={state['cards']}, "
                f"сохранено={state['saved']}, ошибок={state['failed']}{eta_suffix}",
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


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "competitors",
        help="Собрать и проанализировать профессиональные снимки резюме конкурентов",
        description=(
            "READ hh.ru: competitors collect --text QUERY [--search-in SCOPE] "
            "[--max-pages N]; локальный отчёт: competitors report [--text QUERY] "
            "[--search-in SCOPE] [--auth-mode MODE] [--top N]."
        ),
    )
    commands = parser.add_subparsers(dest="competitors_command", required=True)

    collect = commands.add_parser("collect", help="Собрать резюме по ключевому слову (READ hh.ru)")
    collect.add_argument("--text", required=True, help="Ключевое слово поиска резюме")
    collect.add_argument(
        "--max-pages",
        type=_positive,
        default=None,
        help="Необязательный safety-cap (по умолчанию — до конца видимой выдачи)",
    )
    collect.add_argument(
        "--resume",
        action="store_true",
        help="Продолжить последний прерванный запуск того же запроса с checkpoint",
    )
    collect.add_argument(
        "--execution-mode",
        choices=("foreground",),
        default="foreground",
        help="Режим выполнения (по умолчанию foreground; background не поддерживается)",
    )
    collect.add_argument(
        "--progress-verbosity",
        type=int,
        choices=(0, 1),
        default=1,
        help="Поток прогресса: 1 — показывать, 0 — только финал/ошибки (по умолчанию 1)",
    )
    collect.add_argument(
        "--items-per-page",
        type=_page_size,
        default=100,
        help="Запрошенный размер страницы hh.ru (по умолчанию 100; для smoke можно 20)",
    )
    collect.add_argument(
        "--search-in",
        choices=("position", "full_text", "keywords"),
        default="position",
        help=(
            "Область поиска --text на hh.ru: position — только желаемая должность "
            "(заголовок резюме), самая узкая и чистая (по умолчанию); "
            "keywords — по ключевым навыкам; full_text — по всему резюме "
            "(должность, навыки, описание опыта, достижения), самая широкая: "
            "запрос вроде «AI» так вытягивает дизайнеров с Adobe Illustrator"
        ),
    )
    collect.add_argument(
        "--auth-mode",
        choices=("anonymous", "authenticated"),
        default="anonymous",
        help=(
            "Сессия браузера: anonymous — чистый контекст без cookie (по умолчанию); "
            "authenticated — загрузить сохранённую сессию из конфига"
        ),
    )
    collect.add_argument(
        "--detail-workers",
        type=_detail_workers,
        default=10,
        help=(
            "Параллельные процессы деталей: 1–1000 (по умолчанию 10; для authenticated требуется 1)"
        ),
    )
    collect.set_defaults(func=run_collect)

    report = commands.add_parser("report", help="Построить локальный отчёт по сохранённой базе")
    report.add_argument("--text", help="Ограничить отчёт одним поисковым запросом")
    report.add_argument(
        "--search-in",
        choices=("position", "full_text", "keywords"),
        help=(
            "Ограничить отчёт одной областью поиска: один и тот же --text в разных "
            "областях — это РАЗНЫЕ выборки (full_text по «AI» тянет дизайнеров "
            "с Adobe Illustrator). Без флага отчёт охватывает все области"
        ),
    )
    report.add_argument(
        "--auth-mode",
        choices=("anonymous", "authenticated"),
        help=(
            "Ограничить отчёт одним режимом сессии: анонимная выдача hh.ru урезана "
            "относительно авторизованной. Без флага отчёт охватывает оба режима"
        ),
    )
    report.add_argument(
        "--top", type=_positive, default=20, help="Число строк в каждом топе (по умолчанию 20)"
    )
    report.set_defaults(func=run_report)


def run_collect(args: argparse.Namespace) -> bool | CommandExitCode:
    from ..apply.antibot import AntiBotChallengeDetected, AntiBotDetection
    from ..browser import goto_hh, launch_context
    from ..competitor_workers import DetailWorkerConfig, DetailWorkerPool
    from ..competitors import (
        CompetitorSearchCard,
        build_competitor_search_url,
        coverage_warning,
        has_next_search_page,
        inspect_search_coverage,
        parse_search_page,
    )
    from ..config import load_config_or_exit
    from ..history import CommandRunBusy, History

    query = args.text.strip()
    if not query:
        raise ValueError("--text не может быть пустым")
    if args.auth_mode == "authenticated" and args.detail_workers != 1:
        raise ValueError("--auth-mode authenticated требует --detail-workers 1")

    config = load_config_or_exit(args.config)
    history = History(args.history)
    require_authentication = args.auth_mode == "authenticated"
    try:
        started = history.begin_competitor_collection(
            query,
            args.max_pages or 0,
            requested_page_size=args.items_per_page,
            auth_mode=args.auth_mode,
            search_in=args.search_in,
            resume=bool(getattr(args, "resume", False)),
        )
    except CommandRunBusy as exc:
        print(f"[FAIL] {exc}")
        return True
    run_id = started["run_id"]
    page_num = started["resume_page"]
    rank_offset_base = started["resume_rank_offset"]
    progress_verbosity = 0 if getattr(args, "quiet", False) else args.progress_verbosity
    quiet = progress_verbosity == 0
    requested_page_size = args.items_per_page

    for recovered in started["recovered"]:
        _progress(
            f"[RECOVERED] run_id={recovered['run_id']} status={recovered['status']}; "
            f"checkpoint: страниц={recovered['pages_fetched']}, "
            f"карточек={recovered['cards_seen']}, сохранено={recovered['details_saved']}; "
            f"причина={recovered['detail']}",
            quiet=quiet,
            level=logging.WARNING,
        )
    if getattr(args, "resume", False):
        if started["resumed_from_run_id"]:
            _progress(
                f"[RESUME] run_id={run_id} from={started['resumed_from_run_id']}; "
                f"начальная страница={page_num + 1}",
                quiet=quiet,
            )
        else:
            _progress(
                f"[INFO] run_id={run_id}: подходящий checkpoint не найден, "
                "начинаем с первой страницы",
                quiet=quiet,
            )

    state = {
        "pages": 0,
        "cards": 0,
        # Cumulative cards from *completed* pages only, kept in sync with
        # last_completed_page. cards_seen (state["cards"]) already includes
        # the in-progress page's cards as soon as they're parsed -- if a
        # checkpoint fires before that page's details finish, resume_page
        # still points at that same unfinished page, and a resume must not
        # double-count its cards into the rank offset (#660, Codex review).
        "cards_completed": 0,
        "saved": 0,
        "failed": 0,
        "last_started_page": None,
        "last_completed_page": None,
        "resume_page": page_num,
        "observed_page_size": started["resume_observed_page_size"],
        "expected_details": None,
    }
    state_lock = threading.Lock()
    checkpoint_lock = threading.Lock()

    def snapshot() -> dict:
        with state_lock:
            return dict(state)

    def checkpoint() -> None:
        # Snapshot and SQLite write are one ordered operation. Without this
        # lock a delayed heartbeat could overwrite a newer completed-page
        # checkpoint written by the main thread (#654 Codex review).
        with checkpoint_lock:
            current = snapshot()
            history.checkpoint_competitor_collection(
                run_id,
                pages_fetched=current["pages"],
                cards_seen=current["cards"],
                details_saved=current["saved"],
                details_failed=current["failed"],
                last_started_page=current["last_started_page"],
                last_completed_page=current["last_completed_page"],
                resume_page=current["resume_page"],
                observed_page_size=current["observed_page_size"],
                cards_seen_completed=current["cards_completed"],
            )

    details_failed = 0
    new = updated = unchanged = 0
    limited = False
    detail_attempts = 0
    coverage = None
    target_pages: int | None = None
    seen_resume_ids: set[str] = set()
    pages_this_run = 0
    started_at = time.monotonic()
    _progress(
        f"[START] run_id={run_id} competitors collect: "
        f"execution_mode={args.execution_mode}, progress_verbosity={progress_verbosity}, "
        f"auth_mode={args.auth_mode}, "
        f"detail_workers={args.detail_workers}, "
        f"headless={'да' if args.headless else 'нет'}, "
        f"запрошено карточек/страницу={requested_page_size}, "
        f"объём={'до ' + str(args.max_pages) + ' стр.' if args.max_pages else 'до конца выдачи'}, "
        f"лимит страниц={args.max_pages if args.max_pages is not None else 'без лимита'}",
        quiet=quiet,
    )

    handled_signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    previous_handlers = {signum: signal.getsignal(signum) for signum in handled_signals}

    def terminate(signum, _frame):  # noqa: ANN001
        raise _SignalTermination(signum)

    for signum in handled_signals:
        signal.signal(signum, terminate)

    caught: BaseException | None = None
    heartbeat: _Heartbeat | None = None
    try:
        with (
            _Heartbeat(
                snapshot,
                checkpoint,
                run_id=run_id,
                quiet=quiet,
                started_at=started_at,
            ) as heartbeat,
            launch_context(
                config.storage_state_file if require_authentication else None,
                headless=args.headless,
                user_agent=config.user_agent,
            ) as context,
        ):
            search_page = context.new_page()
            worker_pool: DetailWorkerPool | None = None
            try:
                while True:
                    with state_lock:
                        state["last_started_page"] = page_num
                        state["resume_page"] = page_num
                    checkpoint()
                    goto_hh(
                        search_page,
                        build_competitor_search_url(
                            query,
                            page_num,
                            items_per_page=requested_page_size,
                            search_in=args.search_in,
                        ),
                    )
                    cards_before = snapshot()["cards"]
                    cards = parse_search_page(
                        search_page,
                        rank_offset=rank_offset_base + cards_before,
                        expected_page_size=requested_page_size,
                        require_authentication=require_authentication,
                    )
                    pages_this_run += 1
                    with state_lock:
                        state["pages"] += 1
                        state["cards"] += len(cards)
                        state["observed_page_size"] = max(
                            state["observed_page_size"] or 0, len(cards)
                        )
                    has_next = has_next_search_page(search_page, page_num)
                    if coverage is None:
                        observed_page_size = snapshot()["observed_page_size"]
                        coverage = inspect_search_coverage(
                            search_page,
                            page_num,
                            observed_page_size=observed_page_size,
                            requested_page_size=requested_page_size,
                        )
                        available_from_here = (
                            max(1, coverage.available_pages - page_num)
                            if coverage.available_pages is not None
                            else None
                        )
                        target_pages = args.max_pages
                        if target_pages is None:
                            target_pages = available_from_here
                        elif available_from_here is not None:
                            target_pages = min(target_pages, available_from_here)
                        if target_pages is not None and observed_page_size:
                            expected_details = (
                                len(cards) + max(0, target_pages - 1) * requested_page_size
                            )
                            _progress(
                                f"[ESTIMATE] run_id={run_id} "
                                + _throttle_estimate(
                                    details=expected_details,
                                    requested_page_size=requested_page_size,
                                    observed_page_size=observed_page_size,
                                    min_delay=config.throttle.min_delay_seconds,
                                    max_delay=config.throttle.max_delay_seconds,
                                    workers=args.detail_workers,
                                ),
                                quiet=quiet,
                            )
                    if target_pages is not None:
                        with state_lock:
                            state["expected_details"] = (
                                state["cards"]
                                + max(0, target_pages - pages_this_run) * requested_page_size
                            )

                    page_cards = []
                    for card in cards:
                        heartbeat.raise_if_failed()
                        if card.resume_id in seen_resume_ids:
                            continue
                        seen_resume_ids.add(card.resume_id)
                        page_cards.append(card)

                    if page_cards:
                        # Sizing from just this page (instead of capping at
                        # args.detail_workers outright) undersizes the pool
                        # for the rest of the run when an early page is
                        # mostly duplicates (e.g. --resume) — #663 review.
                        # grow() is additive/idempotent, so re-evaluating the
                        # target on every page lets the pool catch up once a
                        # later page proves there is more work than workers.
                        target_workers = min(args.detail_workers, state["cards"])
                        if worker_pool is None:
                            worker_pool = DetailWorkerPool(
                                target_workers,
                                DetailWorkerConfig(
                                    storage_state_file=(
                                        str(config.storage_state_file)
                                        if require_authentication
                                        else None
                                    ),
                                    headless=args.headless,
                                    user_agent=config.user_agent,
                                    min_delay_seconds=config.throttle.min_delay_seconds,
                                    max_delay_seconds=config.throttle.max_delay_seconds,
                                    require_authentication=require_authentication,
                                ),
                            )
                            worker_pool.start()
                        elif target_workers > worker_pool.size:
                            worker_pool.grow(target_workers)
                        _progress(
                            f"[WORKERS] run_id={run_id} запущено={worker_pool.size}",
                            quiet=quiet,
                        )

                    pending: dict[int, CompetitorSearchCard] = {}
                    if worker_pool is not None:
                        for card in page_cards:
                            task_id = detail_attempts
                            detail_attempts += 1
                            pending[task_id] = card
                            worker_pool.submit(task_id, card)

                    while pending:
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
                            raise AntiBotChallengeDetected(
                                AntiBotDetection(
                                    signal=result["antibot_signal"],
                                    detail=result["antibot_detail"],
                                )
                            )
                        if kind == "error":
                            details_failed += 1
                            with state_lock:
                                state["failed"] = details_failed
                            _progress(
                                f"[WARN] run_id={run_id} резюме rank={card.rank} "
                                f"не сохранено: {result['error_type']}: {result['error']}",
                                quiet=quiet,
                                level=logging.WARNING,
                            )
                            continue
                        outcome = history.upsert_competitor_resume(
                            result["payload"],
                            search_query=query,
                            search_rank=card.rank,
                            search_in=args.search_in,
                            auth_mode=args.auth_mode,
                        )
                        with state_lock:
                            state["saved"] += 1
                        if outcome == "new":
                            new += 1
                        elif outcome == "updated":
                            updated += 1
                        else:
                            unchanged += 1

                    with state_lock:
                        state["last_completed_page"] = page_num
                        state["cards_completed"] = state["cards"]
                        state["resume_page"] = page_num + 1 if has_next else None
                        if not has_next or _page_cap_reached(
                            args.max_pages, pages_this_run, has_next
                        ):
                            state["expected_details"] = state["saved"] + state["failed"]
                    checkpoint()
                    current = snapshot()
                    eta = _observed_eta(current, elapsed=time.monotonic() - started_at)
                    eta_suffix = f", {eta}" if eta else ""
                    _progress(
                        f"[PROGRESS] run_id={run_id} страница={page_num + 1}, "
                        f"карточек={current['cards']}, "
                        f"деталей={current['saved'] + current['failed']}, "
                        f"новых/обновлено={new + updated}, ошибок={current['failed']}"
                        f"{eta_suffix}",
                        quiet=quiet,
                    )
                    if not has_next:
                        break
                    if _page_cap_reached(args.max_pages, pages_this_run, has_next):
                        limited = True
                        break
                    page_num += 1
            except BaseException as exc:
                if worker_pool is not None:
                    worker_pool.close(
                        terminate=not isinstance(exc, (KeyboardInterrupt, _SignalTermination))
                    )
                raise
            else:
                if worker_pool is not None:
                    worker_pool.close()
    except BaseException as exc:
        caught = exc
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    current = snapshot()
    elapsed_label = _format_elapsed(time.monotonic() - started_at)
    if caught is not None:
        status = (
            "partial"
            if current["pages"]
            or current["cards"]
            or current["saved"]
            or current["failed"]
            or current["last_started_page"] is not None
            else "failed"
        )
        if isinstance(caught, KeyboardInterrupt):
            code = CommandExitCode.SIGINT.value
        elif isinstance(caught, _SignalTermination):
            code = 128 + caught.signum
        else:
            code = 1
        detail = f"{type(caught).__name__}: {caught}"[:1000]
        history.finish_competitor_collection(
            run_id,
            status=status,
            pages_fetched=current["pages"],
            cards_seen=current["cards"],
            details_saved=current["saved"],
            details_failed=current["failed"],
            detail=detail,
            exit_code=code,
            resume_page=current["resume_page"],
            last_started_page=current["last_started_page"],
            last_completed_page=current["last_completed_page"],
            observed_page_size=current["observed_page_size"],
            cards_seen_completed=current["cards_completed"],
        )
        last_page = (
            current["last_completed_page"] + 1
            if current["last_completed_page"] is not None
            else "нет"
        )
        _progress(
            f"[STOP] run_id={run_id} код завершения={code}; checkpoint: "
            f"завершённая страница={last_page}, страниц={current['pages']}, "
            f"карточек={current['cards']}, сохранено={current['saved']}, "
            f"ошибок={current['failed']}, время={elapsed_label}; причина={detail}",
            quiet=quiet,
            level=logging.ERROR if code == 1 else logging.WARNING,
            always=True,
        )
        if isinstance(caught, KeyboardInterrupt):
            return CommandExitCode.SIGINT
        if isinstance(caught, _SignalTermination):
            if caught.signum == signal.SIGTERM:
                return CommandExitCode.SIGTERM
            return CommandExitCode.SIGHUP
        raise caught

    status = _collection_status(details_failed=details_failed, limited=limited)
    finish_detail = f"limited_by_max_pages={args.max_pages}" if limited else None
    exit_code = 1 if details_failed else 0
    history.finish_competitor_collection(
        run_id,
        status=status,
        pages_fetched=current["pages"],
        cards_seen=current["cards"],
        details_saved=current["saved"],
        details_failed=current["failed"],
        detail=finish_detail,
        exit_code=exit_code,
        resume_page=current["resume_page"] if limited else None,
        last_started_page=current["last_started_page"],
        last_completed_page=current["last_completed_page"],
        observed_page_size=current["observed_page_size"],
        cards_seen_completed=current["cards_completed"],
    )
    total_results = coverage.total_results if coverage else None
    available_pages = coverage.available_pages if coverage else None
    if coverage is not None:
        coverage = replace(coverage, observed_page_size=current["observed_page_size"])
    total_label = total_results if total_results is not None else "не подтверждено"
    pages_label = available_pages if available_pages is not None else "не подтверждено"
    page_size_label = current["observed_page_size"] or "не подтверждено"
    _progress(
        f"Конкуренты: run_id={run_id}, заявлено hh.ru {total_label}, "
        f"доступно страниц {pages_label}, фактически карточек/страницу {page_size_label}, "
        f"просмотрено страниц {current['pages']}, увидено карточек {current['cards']}, "
        f"сохранено уникальных {current['saved']}, новых {new}, обновлено {updated}, "
        f"без изменений {unchanged}, ошибок {current['failed']}, "
        f"код завершения={exit_code}, время={elapsed_label}",
        quiet=quiet,
        always=True,
    )
    warning = coverage_warning(coverage) if coverage else None
    if warning:
        _progress(f"[WARN] run_id={run_id} {warning}", quiet=quiet, level=logging.WARNING)
    if limited:
        _progress(
            f"[WARN] run_id={run_id} выдача ограничена --max-pages={args.max_pages}; "
            f"следующий checkpoint: страница={current['resume_page'] + 1}",
            quiet=quiet,
            level=logging.WARNING,
        )
    return details_failed > 0


def run_report(args: argparse.Namespace) -> None:
    from ..competitors import report_competitors
    from ..history import History

    query = args.text.strip() if args.text else None
    if args.text is not None and not query:
        raise ValueError("--text не может быть пустым")
    search_in = getattr(args, "search_in", None)
    auth_mode = getattr(args, "auth_mode", None)
    if query is None and (search_in or auth_mode):
        # Область поиска — свойство одной выборки, а не всей базы: без --text
        # фильтровать нечего, и молча игнорировать флаг нельзя.
        raise ValueError("--search-in/--auth-mode требуют --text")
    history = History(args.history)
    rows = history.list_competitor_resumes(query, search_in=search_in, auth_mode=auth_mode)
    limited = history.count_limited_competitor_runs(query, search_in=search_in, auth_mode=auth_mode)
    print(report_competitors(rows, top=args.top, limited_runs=limited))

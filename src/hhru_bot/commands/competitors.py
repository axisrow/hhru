"""Collect and report applicant-visible competitor resumes (#578)."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import asdict

from playwright.sync_api import Error as PlaywrightError

from ..exit_codes import CommandExitCode

logger = logging.getLogger("hhru_bot.competitors")


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть >= 1")
    return parsed


def _page_cap_reached(max_pages: int | None, pages_fetched: int, has_next: bool) -> bool:
    return has_next and max_pages is not None and pages_fetched >= max_pages


def _collection_status(*, details_failed: int, limited: bool) -> str:
    if limited:
        return "limited"
    return "partial" if details_failed else "complete"


def _progress(message: str, *, quiet: bool, level: int = logging.INFO) -> None:
    if not quiet:
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
    ):
        self.snapshot = snapshot
        self.checkpoint = checkpoint
        self.run_id = run_id
        self.quiet = quiet
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
                )
                return
            state = self.snapshot()
            page = state["last_started_page"]
            page_label = page + 1 if page is not None else "не начата"
            _progress(
                f"[HEARTBEAT] run_id={self.run_id} competitors collect жив: "
                f"страница={page_label}, карточек={state['cards']}, "
                f"сохранено={state['saved']}, ошибок={state['failed']}",
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
        help="Собрать и проанализировать обезличенные резюме конкурентов",
        description=(
            "READ hh.ru: competitors collect --text QUERY [--max-pages N]; "
            "локальный отчёт: competitors report [--text QUERY] [--top N]."
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
    collect.set_defaults(func=run_collect)

    report = commands.add_parser("report", help="Построить локальный отчёт по сохранённой базе")
    report.add_argument("--text", help="Ограничить отчёт одним поисковым запросом")
    report.add_argument(
        "--top", type=_positive, default=20, help="Число строк в каждом топе (по умолчанию 20)"
    )
    report.set_defaults(func=run_report)


def run_collect(args: argparse.Namespace) -> bool | CommandExitCode:
    from ..browser import goto_hh, launch_context
    from ..competitors import (
        CompetitorResumeIndeterminate,
        build_competitor_search_url,
        coverage_warning,
        fetch_competitor_resume,
        has_next_search_page,
        inspect_search_coverage,
        parse_search_page,
    )
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import Throttle

    query = args.text.strip()
    if not query:
        raise ValueError("--text не может быть пустым")

    config = load_config_or_exit(args.config)
    history = History(args.history)
    throttle = Throttle(config.throttle, history)
    started = history.begin_competitor_collection(
        query, args.max_pages or 0, resume=bool(getattr(args, "resume", False))
    )
    run_id = started["run_id"]
    page_num = started["resume_page"]
    rank_offset_base = started["resume_rank_offset"]
    quiet = getattr(args, "quiet", False)

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
        "saved": 0,
        "failed": 0,
        "last_started_page": None,
        "last_completed_page": None,
        "resume_page": page_num,
        "observed_page_size": started["resume_observed_page_size"],
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
            )

    details_failed = 0
    new = updated = unchanged = 0
    limited = False
    detail_attempts = 0
    coverage = None
    seen_resume_ids: set[str] = set()
    pages_this_run = 0
    _progress(
        f"[START] run_id={run_id} competitors collect: "
        f"headless={'да' if args.headless else 'нет'}, "
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
            _Heartbeat(snapshot, checkpoint, run_id=run_id, quiet=quiet) as heartbeat,
            launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context,
        ):
            search_page = context.new_page()
            detail_page = context.new_page()
            while True:
                with state_lock:
                    state["last_started_page"] = page_num
                    state["resume_page"] = page_num
                checkpoint()
                goto_hh(search_page, build_competitor_search_url(query, page_num))
                cards_before = snapshot()["cards"]
                cards = parse_search_page(search_page, rank_offset=rank_offset_base + cards_before)
                pages_this_run += 1
                with state_lock:
                    state["pages"] += 1
                    state["cards"] += len(cards)
                    if state["observed_page_size"] is None:
                        state["observed_page_size"] = len(cards)
                has_next = has_next_search_page(search_page, page_num)
                if coverage is None:
                    observed_page_size = snapshot()["observed_page_size"]
                    coverage = inspect_search_coverage(
                        search_page, page_num, observed_page_size=observed_page_size
                    )

                for card in cards:
                    heartbeat.raise_if_failed()
                    if card.resume_id in seen_resume_ids:
                        continue
                    seen_resume_ids.add(card.resume_id)
                    if detail_attempts:
                        _progress(
                            f"[WARN] run_id={run_id} пауза троттлинга перед следующей карточкой",
                            quiet=quiet,
                            level=logging.WARNING,
                        )
                        throttle.wait("между карточками резюме конкурентов")
                    detail_attempts += 1
                    try:
                        snapshot_row = fetch_competitor_resume(detail_page, card)
                        payload = asdict(snapshot_row)
                        payload["content_hash"] = snapshot_row.content_hash()
                        outcome = history.upsert_competitor_resume(
                            payload,
                            search_query=query,
                            search_rank=card.rank,
                        )
                    except (CompetitorResumeIndeterminate, PlaywrightError, ValueError) as exc:
                        details_failed += 1
                        with state_lock:
                            state["failed"] = details_failed
                        _progress(
                            f"[WARN] run_id={run_id} резюме rank={card.rank} не сохранено: {exc}",
                            quiet=quiet,
                            level=logging.WARNING,
                        )
                        continue
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
                    state["resume_page"] = page_num + 1 if has_next else None
                checkpoint()
                current = snapshot()
                _progress(
                    f"[PROGRESS] run_id={run_id} страница={page_num + 1}, "
                    f"карточек={current['cards']}, деталей={current['saved'] + current['failed']}, "
                    f"новых/обновлено={new + updated}, ошибок={current['failed']}",
                    quiet=quiet,
                )
                if not has_next:
                    break
                if _page_cap_reached(args.max_pages, pages_this_run, has_next):
                    limited = True
                    break
                page_num += 1
    except BaseException as exc:
        caught = exc
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    current = snapshot()
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
            f"ошибок={current['failed']}; причина={detail}",
            quiet=quiet,
            level=logging.ERROR if code == 1 else logging.WARNING,
        )
        if isinstance(caught, KeyboardInterrupt):
            return CommandExitCode.SIGINT
        if isinstance(caught, _SignalTermination):
            if caught.signum == signal.SIGTERM:
                return CommandExitCode.SIGTERM
            return CommandExitCode.SIGHUP
        raise caught

    status = _collection_status(details_failed=details_failed, limited=limited)
    detail = "limited_by_max_pages=1" if limited else None
    exit_code = 1 if details_failed else 0
    history.finish_competitor_collection(
        run_id,
        status=status,
        pages_fetched=current["pages"],
        cards_seen=current["cards"],
        details_saved=current["saved"],
        details_failed=current["failed"],
        detail=detail,
        exit_code=exit_code,
        resume_page=current["resume_page"] if limited else None,
        last_started_page=current["last_started_page"],
        last_completed_page=current["last_completed_page"],
        observed_page_size=current["observed_page_size"],
    )
    total_results = coverage.total_results if coverage else None
    available_pages = coverage.available_pages if coverage else None
    total_label = total_results if total_results is not None else "не подтверждено"
    pages_label = available_pages if available_pages is not None else "не подтверждено"
    page_size_label = current["observed_page_size"] or "не подтверждено"
    _progress(
        f"Конкуренты: run_id={run_id}, заявлено hh.ru {total_label}, "
        f"доступно страниц {pages_label}, фактически карточек/страницу {page_size_label}, "
        f"просмотрено страниц {current['pages']}, увидено карточек {current['cards']}, "
        f"сохранено уникальных {current['saved']}, новых {new}, обновлено {updated}, "
        f"без изменений {unchanged}, ошибок {current['failed']}, код завершения={exit_code}",
        quiet=quiet,
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
    history = History(args.history)
    rows = history.list_competitor_resumes(query)
    limited = history.count_limited_competitor_runs(query)
    print(report_competitors(rows, top=args.top, limited_runs=limited))

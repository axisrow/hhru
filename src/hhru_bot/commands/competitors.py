"""Collect and report applicant-visible competitor resumes (#578)."""

from __future__ import annotations

import argparse
import threading
from dataclasses import asdict

from playwright.sync_api import Error as PlaywrightError


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


def _progress(message: str, *, quiet: bool) -> None:
    if not quiet:
        print(message, flush=True)


class _Heartbeat:
    def __init__(self, state, *, quiet: bool):
        self.state = state
        self.quiet = quiet
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(45):
            _progress(
                "[HEARTBEAT] competitors collect жив: "
                f"страница={self.state['page']}, карточек={self.state['cards']}, "
                f"сохранено={self.state['saved']}, ошибок={self.state['failed']}",
                quiet=self.quiet,
            )

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
    collect.set_defaults(func=run_collect)

    report = commands.add_parser("report", help="Построить локальный отчёт по сохранённой базе")
    report.add_argument("--text", help="Ограничить отчёт одним поисковым запросом")
    report.add_argument(
        "--top", type=_positive, default=20, help="Число строк в каждом топе (по умолчанию 20)"
    )
    report.set_defaults(func=run_report)


def run_collect(args: argparse.Namespace) -> bool:
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
    run_id = history.start_competitor_collection(query, args.max_pages or 0)

    pages_fetched = 0
    cards_seen = 0
    details_saved = 0
    details_failed = 0
    new = updated = unchanged = 0
    limited = False
    detail_attempts = 0
    page_num = 0
    coverage = None
    seen_resume_ids: set[str] = set()
    state = {"page": 0, "cards": 0, "saved": 0, "failed": 0}
    quiet = getattr(args, "quiet", False)
    _progress(
        "[START] competitors collect: "
        f"headless={'да' if args.headless else 'нет'}, "
        f"лимит страниц={args.max_pages if args.max_pages is not None else 'без лимита'}",
        quiet=quiet,
    )

    try:
        with (
            _Heartbeat(state, quiet=quiet),
            launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context,
        ):
            search_page = context.new_page()
            detail_page = context.new_page()
            while True:
                goto_hh(search_page, build_competitor_search_url(query, page_num))
                cards = parse_search_page(search_page, rank_offset=cards_seen)
                pages_fetched += 1
                cards_seen += len(cards)
                state.update(page=page_num + 1, cards=cards_seen)
                has_next = has_next_search_page(search_page, page_num)
                if page_num == 0:
                    coverage = inspect_search_coverage(search_page, page_num)

                for card in cards:
                    if card.resume_id in seen_resume_ids:
                        continue
                    seen_resume_ids.add(card.resume_id)
                    if detail_attempts:
                        _progress("[WARN] пауза троттлинга перед следующей карточкой", quiet=quiet)
                        throttle.wait("между карточками резюме конкурентов")
                    detail_attempts += 1
                    try:
                        snapshot = fetch_competitor_resume(detail_page, card)
                        payload = asdict(snapshot)
                        payload["content_hash"] = snapshot.content_hash()
                        outcome = history.upsert_competitor_resume(
                            payload,
                            search_query=query,
                            search_rank=card.rank,
                        )
                    except (CompetitorResumeIndeterminate, PlaywrightError, ValueError) as exc:
                        details_failed += 1
                        state["failed"] = details_failed
                        _progress(
                            f"[WARN] резюме rank={card.rank} не сохранено: {exc}", quiet=quiet
                        )
                        continue
                    details_saved += 1
                    state["saved"] = details_saved
                    if outcome == "new":
                        new += 1
                    elif outcome == "updated":
                        updated += 1
                    else:
                        unchanged += 1

                _progress(
                    f"[PROGRESS] страница={page_num + 1}, карточек={cards_seen}, "
                    f"деталей={details_saved + details_failed}, новых/обновлено={new + updated}, "
                    f"ошибок={details_failed}",
                    quiet=quiet,
                )
                if not has_next:
                    break
                if _page_cap_reached(args.max_pages, pages_fetched, has_next):
                    limited = True
                    break
                page_num += 1
    except BaseException as exc:
        status = "partial" if details_saved else "failed"
        history.finish_competitor_collection(
            run_id,
            status=status,
            pages_fetched=pages_fetched,
            cards_seen=cards_seen,
            details_saved=details_saved,
            details_failed=details_failed,
            detail=f"{type(exc).__name__}: {exc}"[:1000],
        )
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
        _progress(
            f"[STOP] код завершения={code}; checkpoint: страниц={pages_fetched}, "
            f"карточек={cards_seen}, сохранено={details_saved}, ошибок={details_failed}; "
            f"причина={type(exc).__name__}: {exc}",
            quiet=quiet,
        )
        raise

    status = _collection_status(details_failed=details_failed, limited=limited)
    detail = "limited_by_max_pages=1" if limited else None
    history.finish_competitor_collection(
        run_id,
        status=status,
        pages_fetched=pages_fetched,
        cards_seen=cards_seen,
        details_saved=details_saved,
        details_failed=details_failed,
        detail=detail,
    )
    total_results = coverage.total_results if coverage else None
    available_pages = coverage.available_pages if coverage else None
    total_label = total_results if total_results is not None else "не подтверждено"
    pages_label = available_pages if available_pages is not None else "не подтверждено"
    _progress(
        "Конкуренты: "
        f"заявлено hh.ru {total_label}, доступно страниц {pages_label}, "
        f"просмотрено страниц {pages_fetched}, увидено карточек {cards_seen}, "
        f"сохранено уникальных {details_saved}, новых {new}, обновлено {updated}, "
        f"без изменений {unchanged}, ошибок {details_failed}",
        quiet=quiet,
    )
    warning = coverage_warning(coverage) if coverage else None
    if warning:
        _progress(f"[WARN] {warning}", quiet=quiet)
    if limited:
        _progress(
            f"[WARN] выдача ограничена --max-pages={args.max_pages}; "
            "на hh.ru подтверждена следующая страница",
            quiet=quiet,
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

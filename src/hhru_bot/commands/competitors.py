"""Collect and report applicant-visible competitor resumes (#578)."""

from __future__ import annotations

import argparse
from dataclasses import asdict

from playwright.sync_api import Error as PlaywrightError


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть >= 1")
    return parsed


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "competitors",
        help="Собрать и проанализировать обезличенные резюме конкурентов",
        description=(
            "READ hh.ru: competitors collect --text QUERY --max-pages N; "
            "локальный отчёт: competitors report [--text QUERY] [--top N]."
        ),
    )
    commands = parser.add_subparsers(dest="competitors_command", required=True)

    collect = commands.add_parser("collect", help="Собрать резюме по ключевому слову (READ hh.ru)")
    collect.add_argument("--text", required=True, help="Ключевое слово поиска резюме")
    collect.add_argument(
        "--max-pages",
        type=_positive,
        default=5,
        help="Максимум страниц выдачи (по умолчанию 5)",
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
        fetch_competitor_resume,
        has_next_search_page,
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
    run_id = history.start_competitor_collection(query, args.max_pages)

    pages_fetched = 0
    cards_seen = 0
    details_saved = 0
    details_failed = 0
    new = updated = unchanged = 0
    limited = False
    detail_attempts = 0

    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            search_page = context.new_page()
            detail_page = context.new_page()
            for page_num in range(args.max_pages):
                goto_hh(search_page, build_competitor_search_url(query, page_num))
                cards = parse_search_page(search_page, rank_offset=cards_seen)
                pages_fetched += 1
                cards_seen += len(cards)
                has_next = has_next_search_page(search_page, page_num)

                for card in cards:
                    if detail_attempts:
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
                        print(f"[WARN] резюме rank={card.rank} не сохранено: {exc}")
                        continue
                    details_saved += 1
                    if outcome == "new":
                        new += 1
                    elif outcome == "updated":
                        updated += 1
                    else:
                        unchanged += 1

                if not has_next:
                    break
                if page_num == args.max_pages - 1:
                    limited = True
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
        raise

    status = "partial" if details_failed else "limited" if limited else "complete"
    detail = "limited_by_max_pages=1" if limited and details_failed else None
    history.finish_competitor_collection(
        run_id,
        status=status,
        pages_fetched=pages_fetched,
        cards_seen=cards_seen,
        details_saved=details_saved,
        details_failed=details_failed,
        detail=detail,
    )
    print(
        "Конкуренты: "
        f"страниц {pages_fetched}, найдено {cards_seen}, сохранено {details_saved}, "
        f"новых {new}, обновлено {updated}, без изменений {unchanged}, ошибок {details_failed}"
    )
    if limited:
        print(
            f"[WARN] выдача ограничена --max-pages={args.max_pages}; "
            "на hh.ru подтверждена следующая страница"
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

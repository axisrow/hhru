"""Collect and report applicant-visible competitor resumes (#578).

CLI-слой (`run_collect`/`run_report`): аргументы, сообщения и коды выхода.
Жизненный цикл сбора (состояние run, checkpoint/финализация, сигналы, цикл
страниц) живёт в пакете `competitor_collection` (#1047).
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from dataclasses import replace
from functools import partial

from ..competitor_collection import (
    CollectionLifecycle,
    CollectionParams,
    CollectionRunState,
    SignalTermination,
    collect_pages,
    collection_status,
    format_duration,
    format_elapsed,
    observed_eta,
    page_cap_reached,
    progress,
    throttle_estimate,
)
from ..exit_codes import CommandExitCode
from ..history import CommandRunBusy, History

# Re-exported for tests and back-compat (#1047): реализации переехали в
# competitor_collection, имена остались на прежнем месте.
__all__ = [
    "_collection_status",
    "_format_duration",
    "_format_elapsed",
    "_observed_eta",
    "_page_cap_reached",
    "_progress",
    "_throttle_estimate",
    "register",
    "run_collect",
    "run_report",
]


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


# Aliases preserved for tests importing the historical underscore names.
_page_cap_reached = page_cap_reached
_collection_status = collection_status
_format_duration = format_duration
_format_elapsed = format_elapsed
_observed_eta = observed_eta
_progress = progress
_throttle_estimate = throttle_estimate


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
    from ..competitors import coverage_warning
    from ..config import load_config_or_exit

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
    started_at = time.monotonic()
    emit = partial(progress, quiet=quiet)

    for recovered in started["recovered"]:
        emit(
            f"[RECOVERED] run_id={recovered['run_id']} status={recovered['status']}; "
            f"checkpoint: страниц={recovered['pages_fetched']}, "
            f"карточек={recovered['cards_seen']}, сохранено={recovered['details_saved']}; "
            f"причина={recovered['detail']}",
            level=logging.WARNING,
        )
    if getattr(args, "resume", False):
        if started["resumed_from_run_id"]:
            emit(
                f"[RESUME] run_id={run_id} from={started['resumed_from_run_id']}; "
                f"начальная страница={page_num + 1}"
            )
        else:
            emit(
                f"[INFO] run_id={run_id}: подходящий checkpoint не найден, "
                "начинаем с первой страницы"
            )

    emit(
        f"[START] run_id={run_id} competitors collect: "
        f"execution_mode={args.execution_mode}, progress_verbosity={progress_verbosity}, "
        f"auth_mode={args.auth_mode}, "
        f"detail_workers={args.detail_workers}, "
        f"headless={'да' if args.headless else 'нет'}, "
        f"запрошено карточек/страницу={requested_page_size}, "
        f"объём={'до ' + str(args.max_pages) + ' стр.' if args.max_pages else 'до конца выдачи'}, "
        f"лимит страниц={args.max_pages if args.max_pages is not None else 'без лимита'}"
    )

    state = CollectionRunState(
        resume_page=page_num,
        observed_page_size=started["resume_observed_page_size"],
    )
    lifecycle = CollectionLifecycle(history, run_id)
    params = CollectionParams(
        query=query,
        search_in=args.search_in,
        auth_mode=args.auth_mode,
        require_authentication=require_authentication,
        requested_page_size=requested_page_size,
        max_pages=args.max_pages,
        detail_workers=args.detail_workers,
        headless=args.headless,
        quiet=quiet,
        user_agent=config.user_agent,
        launch_storage_state=config.storage_state_file if require_authentication else None,
        worker_storage_state=(str(config.storage_state_file) if require_authentication else None),
        min_delay_seconds=config.throttle.min_delay_seconds,
        max_delay_seconds=config.throttle.max_delay_seconds,
        initial_page=page_num,
        rank_offset_base=rank_offset_base,
        started_at=started_at,
    )
    outcome = collect_pages(params, state, lifecycle, emit=emit)
    current = outcome.snapshot
    elapsed_label = format_elapsed(outcome.elapsed_seconds)

    if outcome.caught is not None:
        caught = outcome.caught
        code = outcome.exit_code
        last_page = (
            current.last_completed_page + 1 if current.last_completed_page is not None else "нет"
        )
        emit(
            f"[STOP] run_id={run_id} код завершения={code}; checkpoint: "
            f"завершённая страница={last_page}, страниц={current.pages}, "
            f"карточек={current.cards}, сохранено={current.saved}, "
            f"ошибок={current.failed}, время={elapsed_label}; причина={outcome.detail}",
            level=logging.ERROR if code == 1 else logging.WARNING,
            always=True,
        )
        if isinstance(caught, KeyboardInterrupt):
            return CommandExitCode.SIGINT
        if isinstance(caught, SignalTermination):
            if caught.signum == signal.SIGTERM:
                return CommandExitCode.SIGTERM
            return CommandExitCode.SIGHUP
        raise caught

    total_results = outcome.coverage.total_results if outcome.coverage else None
    available_pages = outcome.coverage.available_pages if outcome.coverage else None
    coverage = outcome.coverage
    if coverage is not None:
        coverage = replace(coverage, observed_page_size=current.observed_page_size)
    total_label = total_results if total_results is not None else "не подтверждено"
    pages_label = available_pages if available_pages is not None else "не подтверждено"
    page_size_label = current.observed_page_size or "не подтверждено"
    emit(
        f"Конкуренты: run_id={run_id}, заявлено hh.ru {total_label}, "
        f"доступно страниц {pages_label}, фактически карточек/страницу {page_size_label}, "
        f"просмотрено страниц {current.pages}, увидено карточек {current.cards}, "
        f"сохранено уникальных {current.saved}, новых {outcome.new}, "
        f"обновлено {outcome.updated}, без изменений {outcome.unchanged}, "
        f"ошибок {current.failed}, "
        f"код завершения={outcome.exit_code}, время={elapsed_label}",
        always=True,
    )
    warning = coverage_warning(coverage) if coverage else None
    if warning:
        emit(f"[WARN] run_id={run_id} {warning}", level=logging.WARNING)
    if outcome.limited:
        emit(
            f"[WARN] run_id={run_id} выдача ограничена --max-pages={args.max_pages}; "
            f"следующий checkpoint: страница={current.resume_page + 1}",
            level=logging.WARNING,
        )
    return outcome.details_failed > 0


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

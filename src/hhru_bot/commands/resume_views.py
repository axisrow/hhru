"""Real employer views of resumes (read-only, #415)."""

from __future__ import annotations

import argparse
import sys

URL = "https://hh.ru/applicant/resumeview/history"


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "resume-views", help="Показать реальные просмотры резюме работодателями"
    )
    p.add_argument("--resume", help="ID резюме (по умолчанию — все резюме)")
    p.add_argument(
        "--limit", type=int, default=100, help="Максимум snapshots на резюме (по умолчанию 100)"
    )
    p.add_argument(
        "--max-pages", type=int, default=5, help="Максимум страниц истории (по умолчанию 5)"
    )
    p.set_defaults(func=run)


def _table(rows: list[dict]) -> None:
    headers = ("Дата", "Работодатель", "Резюме")
    body = [(r["viewed_at"], r.get("employer") or "(скрыт)", r["resume_id"]) for r in rows]
    widths = [max([len(h)] + [len(str(x[i])) for x in body]) for i, h in enumerate(headers)]
    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(border)
    print("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    print(border)
    for row in body:
        print("| " + " | ".join(str(x).ljust(widths[i]) for i, x in enumerate(row)) + " |")
    print(border)


def run(args: argparse.Namespace) -> None:
    from ..browser import goto_hh, launch_context, require_authenticated_page
    from ..config import load_config_or_exit
    from ..history import History
    from ..resume_views import parse_resume_view_history, parse_resume_view_history_dom

    if args.limit < 1 or args.max_pages < 1:
        raise ValueError("limit и max-pages должны быть >= 1")
    config = load_config_or_exit(args.config)
    history = History(args.history)
    if args.resume is None:
        resumes = config.resumes
    else:
        from ._common import resolve_resume

        try:
            resumes = [resolve_resume(config, args.resume)]
        except Exception as exc:
            print(f"[FAIL] резюме не найдено: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    if not resumes:
        print("[FAIL] резюме не найдено", file=sys.stderr)
        raise SystemExit(1)

    fetched: list[dict] = []
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        for resume in resumes:
            # Route accepts resume_id as a query parameter on current hh.ru; no
            # direct HTTP request is made, preserving the browser boundary.
            for page_num in range(args.max_pages):
                goto_hh(page, f"{URL}?resume={resume.resume_id}&page={page_num}")
                require_authenticated_page(page)
                try:
                    rows = parse_resume_view_history(
                        page.content(), resume.resume_id, limit=args.limit
                    )
                except (ValueError, TypeError) as exc:
                    rows = parse_resume_view_history_dom(
                        page, resume.resume_id, limit=args.limit
                    )
                    if not rows:
                        print(f"[FAIL] история просмотров не подтверждена: {exc}", file=sys.stderr)
                        raise SystemExit(1) from exc
                fetched.extend(rows)
                if not rows or len(rows) < args.limit:
                    break

    inserted = history.record_resume_views(fetched)
    stored = history.resume_views(resumes[0].resume_id if args.resume is not None else None)
    print(f"Просмотры резюме: всего {len(stored)}, новых {inserted}")
    if not stored:
        print("(нет подтверждённых просмотров)")
        return
    print("Тренд по дням:")
    by_day = {}
    for row in stored:
        day = str(row["viewed_at"])[:10]
        by_day[day] = by_day.get(day, 0) + 1
    for day in sorted(by_day, reverse=True):
        print(f"  {day}: {by_day[day]}")
    print("Топ работодателей:")
    by_employer = {}
    for row in stored:
        name = row.get("employer") or "(скрыт)"
        by_employer[name] = by_employer.get(name, 0) + 1
    for name, count in sorted(by_employer.items(), key=lambda item: (-item[1], item[0]))[:10]:
        print(f"  {count}  {name}")
    _table(stored[: args.limit])

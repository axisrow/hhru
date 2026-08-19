"""Fill an external form in a human-reviewable dry-run only (#276)."""

from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from ..external_forms import scan_form
from ..external_forms.detect import apply_answers, resolve_answers
from ..history import History
from ..logging_setup import LOG_DIR


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "fill-form", help="Заполнить внешнюю анкету и сохранить дамп без отправки"
    )
    p.add_argument("--url", required=True, help="Явный URL внешней формы")
    p.add_argument("--resume", required=True, help="ID резюме из конфига")
    p.add_argument(
        "--dry-run", action="store_true", help="Обязательный режим: без submit и навигации формы"
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    if not args.dry_run:
        print("[FAIL] fill-form требует обязательный --dry-run; submit не поддерживается")
        return True
    parsed = urlparse(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        print("[FAIL] URL должен быть явным HTTP(S)-адресом")
        return True

    from ..browser import launch_context
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    history = History(args.history)
    answers = history.get_profile_answers()
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        # Do not follow redirects away from the explicitly supplied external host.
        if urlparse(page.url).netloc != parsed.netloc:
            print("[FAIL] внешний URL перенаправил на другой домен; заполнение отменено")
            return True
        scan = scan_form(page)
        if scan.indeterminate:
            print(f"[FAIL] [indeterminate] {scan.reason}")
            return True
        # Account-profile rows are already structured facts (manual rows win
        # over hh.ru rows).  Keep the LLM optional and let it select only from
        # these facts; it must never generate a new form answer.
        llm = None
        if getattr(config, "ai", None) is not None and answers:
            try:
                from ..ai.llm_client import LLMClient

                llm = LLMClient(config.ai)
            except Exception as exc:  # noqa: BLE001 — конструктор внешнего клиента не
                # должен ронять fill_form; та же деградация, что при сбое самого
                # чата (см. detect.py::match_answer_llm) и в ai/letters.py.
                print(f"[WARN] LLM-сопоставление отключено: {exc}")
        resolved_answers, llm_matched = resolve_answers(
            scan, answers, known_data=answers, client=llm
        )
        ok, missing = apply_answers(page, scan, resolved_answers)
        for label in sorted(llm_matched):
            print(f"[INFO] LLM-сопоставление: {label!r} -> {resolved_answers[label]!r}")
        out = Path(LOG_DIR)
        out.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        host_slug = re.sub(r"[^a-z0-9]+", "-", parsed.netloc.casefold()).strip("-")
        slug = f"external_form_dry_run_{timestamp}_{host_slug}"
        html = out / f"{slug}.html"
        png = out / f"{slug}.png"
        html.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(png), full_page=True)
        print(f"[DRY-RUN] Дамп сохранён: {html}, {png}")
        if not ok:
            print(
                "[FAIL] [indeterminate] Не заполнены обязательные/неподтверждённые поля: "
                + ", ".join(missing)
            )
            return True
        print("[DRY-RUN] Поля заполнены по профилю. Submit не выполнялся.")
        return False

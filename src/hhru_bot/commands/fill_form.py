"""Fill an external form in a human-reviewable dry-run only (#276)."""

from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from ..external_forms import scan_form
from ..external_forms.detect import apply_answers
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
        ok, missing = apply_answers(page, scan, answers)
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
        print("[DRY-RUN] Поля заполнены по точному совпадению профиля. Submit не выполнялся.")
        return False

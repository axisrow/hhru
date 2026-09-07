"""Команда export-resume: read-only экспорт живого резюме в JSON (#1023).

Читает отрисованную страницу резюме (позиция, зарплата, контакты, опыт,
образование, навыки, языки, о себе, дополнительные блоки) и скачивает
оригиналы фото из общей галереи аккаунта в ``data/exports/``. Ничего не
мутирует на hh.ru: только goto и чтение DOM (+ read-only клик-карандаш
инвентаря фото). Недоступные секции честно перечисляются в отчёте
(``[WARN]``), значения не выдумываются.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from ._common import add_common_args, resumes_from_args


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "export-resume",
        help="Экспортировать живое резюме и фото в локальный JSON (read-only)",
        description=(
            "Снимает живое резюме целиком (позиция, зарплата, опыт, образование, "
            "навыки, языки, о себе, контакты, портфолио и другие блоки) и "
            "скачивает оригиналы фото из галереи аккаунта в data/exports/. "
            "READ hh.ru / WRITE-local: на hh.ru только чтение; недоступные "
            "секции честно перечисляются в отчёте."
        ),
    )
    add_common_args(parser, max_pages_default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data") / "exports",
        help="Каталог для экспорта (по умолчанию data/exports)",
    )
    parser.add_argument(
        "--no-photos",
        action="store_true",
        help="Не читать галерею и не скачивать фото",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool:
    from ..apply.antibot import AntiBotChallengeDetected
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..export_resume import export_resume_on_hh

    config = load_config_or_exit(args.config)
    resumes = resumes_from_args(config, args)
    if not resumes:
        print("[FAIL] В конфиге нет резюме для экспорта")
        return True

    failures = 0
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        for resume in resumes:
            # Изоляция per-resume: сбой браузера/анти-бота на одном резюме не
            # должен обрывать батч без вердикта — остальные получают свой
            # [FAIL]/[OK], как в других мульти-resume командах.
            try:
                result = export_resume_on_hh(
                    context,
                    resume,
                    output_dir=args.output,
                    with_photos=not args.no_photos,
                )
            except (PlaywrightError, AntiBotChallengeDetected) as exc:
                failures += 1
                reason = str(exc).split("\n")[0][:300]
                print(f"[FAIL] {resume.id} — браузерный сбой: {reason}")
                continue
            if not result.success:
                failures += 1
                print(f"[FAIL] {resume.id} — {result.reason}")
                continue
            print(f"[OK] {resume.id} — экспорт: {result.path}")
            payload = result.payload
            position = payload.get("position", {})
            print(f"[INFO] Позиция: {position.get('title') or '<недоступно>'}")
            print(f"[INFO] Зарплата: {position.get('salary_text') or '<не указана>'}")
            print(
                f"[INFO] Опыт: {len(payload.get('experience', {}).get('companies', []))} компаний"
            )
            print(f"[INFO] Образование: {len(payload.get('education', []))} записей")
            print(f"[INFO] Навыки: {len(payload.get('skills', []))}")
            if payload.get("photos"):
                print(
                    "[INFO] Фото: "
                    + ", ".join(
                        f"{record['photo_id']} ({record['status']})" for record in payload["photos"]
                    )
                )
            for note in result.unavailable:
                print(f"[WARN] недоступно: {note}")
    return failures > 0

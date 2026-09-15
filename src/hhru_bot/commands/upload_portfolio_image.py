"""Команда upload-portfolio-image: загрузить изображение в галерею портфолио.

Галерея портфолио — аккаунтовая (/applicant/gallery, кнопка «Добавить
работу»); из неё резюме собирает блок «Портфолио» (#1121). WRITE-hh-ru:
по умолчанию только dry-run (read-only осмотр мишени загрузки); боевой
запуск требует --force или интерактивного подтверждения.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "upload-portfolio-image",
        help="Загрузить изображение в галерею портфолио аккаунта",
        description=(
            "Передаёт файл в скрытый file-input кнопки «Добавить работу» "
            "(/applicant/gallery). WRITE-hh-ru: по умолчанию только dry-run "
            "(осмотр мишени загрузки); боевой запуск требует --force или "
            "интерактивного подтверждения. Успех — только появление новой "
            "карточки gallery-image-{id}; её id печатается для --portfolio."
        ),
    )
    parser.add_argument("--photo", required=True, type=Path, help="Путь к файлу jpg/jpeg/png")
    parser.add_argument(
        "--dry-run", action="store_true", help="Осмотреть мишень загрузки, ничего не загружая"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить боевую загрузку")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace):
    from ..config import load_config_or_exit
    from ..resume_photo import validate_photo

    try:
        photo = validate_photo(args.photo)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return True
    config = load_config_or_exit(args.config)
    if not args.dry_run and not confirm_write(
        args.force, prompt="Загрузить изображение в галерею портфолио?"
    ):
        print("[FAIL] Боевой режим требует --force или интерактивного подтверждения.")
        return True

    from ..browser import launch_context
    from ..portfolio_upload import upload_portfolio_image

    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        result = upload_portfolio_image(context.new_page(), photo.path, dry_run=args.dry_run)
    if not result.success:
        prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
        print(f"{prefix} {result.reason}")
        return True
    if args.dry_run:
        print(f"[DRY-RUN] {result.reason}")
        print("[INFO] Ничего не загружено.")
    else:
        print(f"[OK] Изображение загружено в галерею портфолио, photo_id={result.photo_id}")
    return False

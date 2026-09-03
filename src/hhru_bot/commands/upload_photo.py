"""Команда upload-photo: загрузить фото в резюме на hh.ru."""

from __future__ import annotations

import argparse
from pathlib import Path

from ._common import ApplyProgress, DurableMutationAttempt, resolve_resume, run_supervised_command
from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "upload-photo",
        help="Загрузить фото в резюме на hh.ru",
        description=(
            "Передаёт файл фото в скрытый file-input блока аватара на странице "
            "резюме. WRITE-hh-ru: по умолчанию только dry-run (read-only осмотр "
            "блока фото); боевой запуск требует --force или интерактивного "
            "подтверждения. Замена существующего фото не поддерживается."
        ),
    )
    parser.add_argument("--resume", required=True, help="Slug из конфига или resume_id HH.ru")
    parser.add_argument("--photo", required=True, type=Path, help="Путь к файлу jpg/jpeg/png")
    parser.add_argument(
        "--dry-run", action="store_true", help="Осмотреть блок фото, ничего не загружая"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить боевую загрузку")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..resume_photo import photo_upload_plan, upload_photo_on_hh, validate_photo

    try:
        photo = validate_photo(args.photo)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return True
    config = load_config_or_exit(args.config)
    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True

    history = History(args.history)
    # Гейт до confirm_write: пользователя нельзя сначала спрашивать подтверждение
    # боя и только потом отказывать по незакрытой неопределённости (ревью #952).
    if not args.dry_run and history.has_unresolved_uncertain(resume.resume_id, "upload_photo"):
        print(
            f"[FAIL] {resume.id} — предыдущая загрузка фото не подтверждена (uncertain). "
            "Проверьте фото на hh.ru вручную перед повтором."
        )
        return True
    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Загрузить фото в резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не загружено."
        )
        return True

    def _body(progress: ApplyProgress) -> bool:
        attempt = (
            None
            if args.dry_run
            else DurableMutationAttempt(history, progress, resume.resume_id, "upload_photo")
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                result = upload_photo_on_hh(
                    context.new_page(),
                    resume,
                    photo,
                    args.dry_run,
                    before_click=attempt.before_click if attempt is not None else None,
                )
        except BaseException as exc:
            if attempt is not None:
                attempt.interrupt(exc)
            raise
        if attempt is not None:
            attempt.finish(result)
        if not result.success:
            prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
            print(f"{prefix} {resume.id} — {result.reason}")
            return True
        if args.dry_run:
            print(f"[DRY-RUN] {photo_upload_plan(photo, resume.id)}")
            print("[INFO] Ничего не загружено.")
        else:
            print(f"[OK] Фото загружено в резюме {resume.id}")
        return False

    return run_supervised_command(
        command=getattr(args, "command", "upload-photo"),
        history=history,
        requested_limit=None,
        body=_body,
    )

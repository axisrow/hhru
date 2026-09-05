"""Команда select-photo: назначить резюме фото из библиотеки hh.ru (#953).

Dry-run — read-only инвентарь: открывает вьюер фото кнопкой-карандашом
(клик не мутирует, бои 2026-09-02/03) и печатает различимые идентификаторы
фото галереи (числовой id из URL). Боевой режим назначает выбранное
``--photo-id`` фото этому резюме; замена существующего фото — тот же путь.
Удаление фото из библиотеки/резюме вне скоупа команды (#953).
"""

from __future__ import annotations

import argparse

from ._common import ApplyProgress, DurableMutationAttempt, resolve_resume, run_supervised_command
from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "select-photo",
        help="Назначить резюме фото из библиотеки hh.ru",
        description=(
            "Открывает вьюер фото на странице резюме кнопкой-карандашом, "
            "показывает библиотеку фото аккаунта и назначает выбранное фото "
            "этому резюме (в том числе замену существующего). WRITE-hh-ru: "
            "по умолчанию dry-run (read-only инвентарь библиотеки); боевой "
            "запуск требует --photo-id и --force или интерактивного "
            "подтверждения."
        ),
    )
    parser.add_argument("--resume", required=True, help="Slug из конфига или resume_id HH.ru")
    parser.add_argument(
        "--photo-id",
        default=None,
        help="Числовой id фото из dry-run инвентаря (обязателен в боевом режиме)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать библиотеку фото, ничего не назначая"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить боевое назначение")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..resume_photo import select_photo_on_hh, select_photo_plan

    if not args.dry_run and not args.photo_id:
        print("[FAIL] Боевой режим требует --photo-id (см. select-photo --dry-run)")
        return True
    if args.photo_id and not args.photo_id.isdigit():
        print(f"[FAIL] --photo-id ожидает числовой id из инвентаря, получено: {args.photo_id}")
        return True
    config = load_config_or_exit(args.config)
    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True

    history = History(args.history)
    # Гейт до confirm_write: нельзя сначала спрашивать подтверждение боя и
    # только потом отказывать по незакрытой неопределённости (прецедент #952).
    if not args.dry_run and history.has_unresolved_uncertain(resume.resume_id, "select_photo"):
        print(
            f"[FAIL] {resume.id} — предыдущее назначение фото не подтверждено (uncertain). "
            "Проверьте фото резюме на hh.ru вручную перед повтором."
        )
        return True
    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Назначить фото {args.photo_id} резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не назначено."
        )
        return True

    def _body(progress: ApplyProgress) -> bool:
        attempt = (
            None
            if args.dry_run
            else DurableMutationAttempt(history, progress, resume.resume_id, "select_photo")
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                result = select_photo_on_hh(
                    context.new_page(),
                    resume,
                    args.photo_id,
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
            print(f"[DRY-RUN] {select_photo_plan(resume.id, args.photo_id)}")
            if result.photos:
                print(f"[INFO] Библиотека фото ({len(result.photos)}):")
                for photo in result.photos:
                    print(f"  photo {photo.photo_id} — {photo.src}")
            else:
                print("[INFO] Библиотека фото пуста.")
            if result.avatar_src:
                print(f"[INFO] Текущее фото резюме: {result.avatar_src}")
            else:
                print("[INFO] У резюме сейчас нет фото.")
            print("[INFO] Ничего не назначено.")
        else:
            print(f"[OK] Резюме {resume.id}: назначено фото {result.assigned_photo_id}")
        return False

    return run_supervised_command(
        command=getattr(args, "command", "select-photo"),
        history=history,
        requested_limit=None,
        body=_body,
    )

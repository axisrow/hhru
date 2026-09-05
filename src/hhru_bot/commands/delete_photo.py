"""Команда delete-photo: скрыть фото из резюме / удалить из библиотеки (#966)."""

from __future__ import annotations

import argparse

from ._common import ApplyProgress, DurableMutationAttempt, resolve_resume, run_supervised_command
from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "delete-photo",
        help="Скрыть фото из резюме или удалить из библиотеки hh.ru",
        description=(
            "По умолчанию скрывает фото из ОДНОГО резюме (пункт «Скрыть "
            "фото из резюме»; фото остаётся в библиотеке, возвращается "
            "select-photo). --from-library необратимо удаляет фото из "
            "библиотеки аккаунта — оно исчезает из ВСЕХ резюме, где "
            "установлено. WRITE-hh-ru: по умолчанию dry-run (read-only "
            "инвентарь библиотеки и пунктов more-меню «Действия с фото»); "
            "боевой запуск требует --photo-id и --force или интерактивного "
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
        "--from-library",
        action="store_true",
        help=(
            "Удалить фото из библиотеки аккаунта (необратимо, бьёт по ВСЕМ "
            "резюме с этим фото), а не только скрыть из одного резюме"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать план и пункты меню, ничего не меняя"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить боевое удаление/скрытие")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..delete_photo import DELETE_ACTION, HIDE_ACTION, delete_photo_on_hh, delete_photo_plan
    from ..history import History
    from ..resume_photo import PHOTO_VIEWPORT

    action = DELETE_ACTION if args.from_library else HIDE_ACTION
    if not args.dry_run and not args.photo_id:
        print("[FAIL] Боевой режим требует --photo-id (см. delete-photo --dry-run)")
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
    if not args.dry_run and history.has_unresolved_uncertain(resume.resume_id, action):
        print(
            f"[FAIL] {resume.id} — предыдущее действие с фото ({action}) не подтверждено "
            "(uncertain). Проверьте фото на hh.ru вручную перед повтором."
        )
        return True
    if args.from_library:
        prompt = (
            f"НЕОБРАТИМО удалить фото {args.photo_id} из библиотеки hh.ru "
            "(оно исчезнет из ВСЕХ резюме, где установлено)?"
        )
    else:
        prompt = f"Скрыть фото {args.photo_id} из резюме '{resume.id}' на hh.ru?"
    if not args.dry_run and not confirm_write(args.force, prompt=prompt):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не изменено."
        )
        return True

    def _body(progress: ApplyProgress) -> bool:
        attempt = (
            None
            if args.dry_run
            else DurableMutationAttempt(history, progress, resume.resume_id, action)
        )
        try:
            with launch_context(
                config.storage_state_file,
                headless=args.headless,
                user_agent=config.user_agent,
                viewport=PHOTO_VIEWPORT,
            ) as context:
                result = delete_photo_on_hh(
                    context.new_page(),
                    resume,
                    args.photo_id,
                    args.from_library,
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
            print(f"[DRY-RUN] {delete_photo_plan(resume.id, args.photo_id, args.from_library)}")
            if result.photos:
                print(f"[INFO] Библиотека фото ({len(result.photos)}):")
                for photo in result.photos:
                    print(f"  photo {photo.photo_id} — {photo.src}")
            else:
                print("[INFO] Библиотека фото пуста.")
            if result.menu_actions:
                print("[INFO] Пункты more-меню «Действия с фото»:")
                for item in result.menu_actions:
                    print(f"  {item.qa} — {item.text!r}")
                if not args.from_library and not any(
                    item.qa.endswith("action-hide") for item in result.menu_actions
                ):
                    print(
                        "[INFO] Пункта «Скрыть фото из резюме» нет: текущий слайд "
                        "не назначен этому резюме (для скрытия выберите --photo-id "
                        "назначенного фото)."
                    )
            print("[INFO] Ничего не удалено и не скрыто.")
        elif args.from_library:
            print(f"[OK] {result.reason}")
        else:
            print(f"[OK] Резюме {resume.id}: {result.reason}")
        return False

    return run_supervised_command(
        command=getattr(args, "command", "delete-photo"),
        history=history,
        requested_limit=None,
        body=_body,
    )

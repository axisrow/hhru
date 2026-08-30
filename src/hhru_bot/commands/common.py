"""CLI command for the simple fields of the resume ``common`` screen (#876)."""

from __future__ import annotations

import argparse

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "common",
        help="Заполнить простые поля экрана common резюме",
        description=(
            "Заполняет через UI firstName, lastName, birthday, gender, phone, area, "
            "metro и citizenship."
        ),
    )
    parser.add_argument("--resume", required=True, help="Slug из конфига или resume_id HH.ru")
    parser.add_argument("--first-name", dest="first_name", help="Имя")
    parser.add_argument("--last-name", dest="last_name", help="Фамилия")
    parser.add_argument("--birthday", help="Дата в формате, который принимает форма hh.ru")
    parser.add_argument("--gender", choices=("male", "female"), help="Пол")
    parser.add_argument("--phone", help="Телефон")
    parser.add_argument("--area", help="Точный leaf города из live-каталога hh.ru")
    parser.add_argument("--metro", help="Точная станция метро")
    parser.add_argument(
        "--citizenship",
        action="append",
        help="Точное гражданство из live-каталога; можно повторять",
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать план без сохранения")
    parser.add_argument("--force", action="store_true", help="Подтвердить запись без prompt")
    parser.set_defaults(func=run)


def _run(args: argparse.Namespace, progress) -> bool:
    from ..browser import BrowserLaunchError, NotAuthenticated, launch_context
    from ..common import CANCEL, CommonValues, open_common_form, read_common, save_common
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ._common import DurableMutationAttempt, resolve_resume

    config = load_config_or_exit(args.config)
    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True
    values = CommonValues(
        first_name=args.first_name,
        last_name=args.last_name,
        birthday=args.birthday,
        gender=args.gender,
        phone=args.phone,
        area=getattr(args, "area", None),
        metro=[args.metro] if getattr(args, "metro", None) is not None else None,
        citizenship=getattr(args, "citizenship", None),
    )
    if not values.provided():
        print("[FAIL] Укажите хотя бы одно поле common")
        return True
    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Сохранить простые поля common резюме '{resume.id}' на hh.ru?"
    ):
        print("[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено.")
        return True
    history = History(args.history)
    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            open_common_form(page, resume)
            read_common(page)
            print("[INFO] Текущие значения common прочитаны через UI.")
            for key, value in values.provided().items():
                print(f"  {key}: {value}")
            if args.dry_run:
                page.locator(CANCEL).first.click()
                print("[DRY-RUN] save не нажат; изменений на hh.ru нет")
                return False
            attempt = DurableMutationAttempt(history, progress, resume.resume_id, "edit_common")
            result = save_common(page, values, before_click=attempt.before_click)
            attempt.finish(result)
    except BrowserLaunchError:
        raise
    except NotAuthenticated:
        # Preserve the supervisor's dedicated SESSION_EXPIRED classification
        # and its actionable login/refresh-token guidance.
        raise
    except Exception as exc:
        if progress.attempted_count:
            progress.finish(exc)
        print(f"[FAIL] {resume.id} — {exc}")
        return True
    if result.success:
        print(f"[OK] Простые поля common резюме '{resume.id}' сохранены.")
        return False
    prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
    print(f"{prefix} {resume.id} — {result.reason}")
    return True


def run(args: argparse.Namespace):
    from ._common import run_single_mutation_command

    return run_single_mutation_command(command="edit_common", args=args, body=_run)

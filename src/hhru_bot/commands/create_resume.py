"""Команда create-resume: создать пустой черновик резюме на hh.ru (#304)."""

from __future__ import annotations

import argparse

from .copy_resume import confirm_write, format_config_snippet


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "create-resume",
        help="Создать пустой черновик резюме на hh.ru",
        description=(
            "Открывает визард hh.ru и создаёт новый черновик резюме. "
            "WRITE-команда: по умолчанию только dry-run; боевой запуск требует "
            "--force или интерактивного подтверждения."
        ),
    )
    p.add_argument("--area", required=True, help="Профобласть первого шага визарда")
    p.add_argument("--title", required=True, help="Желаемая должность первого шага")
    p.add_argument("--dry-run", action="store_true", help="Показать план без создания")
    p.add_argument("--force", action="store_true", help="Подтвердить боевое создание")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..create_resume import create_resume_on_hh
    from ..history import History

    config = load_config_or_exit(args.config)
    history = History(args.history)
    # --dry-run is an explicit safety promise and always wins over --force.
    # This also makes scripted invocations safe when a shared command builder
    # supplies --force while toggling dry-run independently.
    dry_run = args.dry_run or not args.force
    if not dry_run and not confirm_write(
        args.force, prompt=f"Создать новое резюме «{args.title}» на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не создано."
        )
        raise SystemExit(1)
    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            result = create_resume_on_hh(
                context.new_page(), area=args.area, title=args.title, dry_run=dry_run
            )
    except Exception as exc:
        history.record_action("account", "account", "create_resume", "failed", f"исключение: {exc}")
        raise
    status = (
        "dry_run"
        if dry_run
        else ("uncertain" if result.uncertain else ("success" if result.success else "failed"))
    )
    history.record_action("account", "account", "create_resume", status, result.reason)
    if not result.success:
        prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
        print(f"{prefix} {result.reason}")
        raise SystemExit(1)
    if dry_run:
        print(f"[DRY-RUN] Создание резюме: area={args.area}, title={args.title}")
        print(f"[INFO] {result.reason}")
    else:
        print(f"[OK] Черновик резюме создан. Новый resume_id: {result.new_resume_id}")
        print(format_config_snippet(result.new_resume_id))

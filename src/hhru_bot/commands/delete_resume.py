"""Команда delete-resume: необратимо удалить одно резюме (#293)."""

from __future__ import annotations

import argparse

from ._audit import action_status, record_resume_action
from .copy_resume import confirm_write


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "delete-resume",
        help="Необратимо удалить одно резюме на hh.ru",
        description=(
            "Удаляет ровно одно резюме через UI hh.ru. --resume обязателен; "
            "по умолчанию выполняется только dry-run."
        ),
    )
    p.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Показать план без удаления (по умолчанию; --force включает боевой режим)",
    )
    p.add_argument("--force", action="store_true", help="Подтвердить необратимое удаление")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..delete_resume import delete_resume_on_hh
    from ..history import History
    from ..responses import NotAuthenticated

    config = load_config_or_exit(args.config)
    # Fail closed: --force is the sole switch that can leave dry-run.
    dry_run = not args.force
    from ._common import resolve_resume

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        raise SystemExit(1) from None
    history = History(args.history)
    if not dry_run and not confirm_write(
        args.force, prompt=f"НЕОБРАТИМО удалить резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не удалено."
        )
        raise SystemExit(1)

    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            result = delete_resume_on_hh(context.new_page(), resume, dry_run)
    except NotAuthenticated as exc:
        if not dry_run:
            record_resume_action(history, resume.resume_id, "delete_resume", "failed", str(exc))
        print(f"[FAIL] {resume.id} — Сессия недействительна: {exc}")
        raise SystemExit(1) from None
    except Exception as exc:
        if not dry_run:
            record_resume_action(
                history, resume.resume_id, "delete_resume", "failed", f"исключение: {exc}"
            )
        raise

    if not dry_run:
        status = action_status(dry_run=False, success=result.success, uncertain=result.uncertain)
        record_resume_action(history, resume.resume_id, "delete_resume", status, result.reason)
    if not result.success:
        prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
        print(f"{prefix} {resume.id} — {result.reason}")
        raise SystemExit(1)
    if dry_run:
        print(f"[DRY-RUN] Резюме {resume.id}: {result.reason}")
        print("[INFO] Ничего не удалено.")
    else:
        print(f"[OK] Резюме {resume.id} удалено")

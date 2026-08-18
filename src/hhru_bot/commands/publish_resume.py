"""Команда publish-resume: опубликовать черновик через UI-клик (#219)."""

from __future__ import annotations

import argparse
import sys


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "publish-resume",
        help="Опубликовать черновик резюме на hh.ru",
        description=(
            "Публикует черновик резюме кликом по кнопке hh.ru. WRITE-hh-ru: "
            "боевой режим требует --force; --dry-run ничего не нажимает."
        ),
    )
    p.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    p.add_argument("--dry-run", action="store_true", help="Проверить состояние без клика")
    p.add_argument("--force", action="store_true", help="Разрешить боевой UI-клик")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..publish_resume import publish_resume_on_hh
    from ..responses import NotAuthenticated

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
    if not args.dry_run and not args.force:
        print("[FAIL] Боевой режим требует --force. Ничего не нажато.")
        sys.exit(1)

    history = History(args.history)
    if not args.dry_run and history.has_unresolved_uncertain(resume.resume_id, "publish_resume"):
        print(
            f"[FAIL] {resume.id} — предыдущая публикация не подтверждена (uncertain). "
            "Проверьте статус резюме на hh.ru вручную перед повтором."
        )
        sys.exit(1)

    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            result = publish_resume_on_hh(context.new_page(), resume, args.dry_run)
    except NotAuthenticated as exc:
        print(f"[FAIL] {resume.id} — Сессия недействительна: {exc}")
        sys.exit(1)

    # actions — журнал взаимодействий с hh.ru, а не всех проверок команды.
    # До клика (identity/state/button guards) внешний эффект невозможен и не
    # должен выглядеть в истории как неудачная попытка публикации.  ``uncertain``
    # означает, что клик уже мог уйти, поэтому такую запись сохраняем.
    if not args.dry_run and (result.success or result.uncertain):
        status = "uncertain" if result.uncertain else "success"
        history.record_action(
            resume.resume_id, resume.resume_id, "publish_resume", status, result.reason
        )

    if not result.success:
        prefix = "[FAIL]" if not result.uncertain else "[FAIL] (uncertain)"
        print(f"{prefix} {resume.id} — {result.reason}")
        sys.exit(1)
    if args.dry_run:
        print(f"[DRY-RUN] Резюме {resume.id}: {result.reason}")
        print("[INFO] Ничего не нажато; изменение видимости — отдельное действие.")
    else:
        visibility = "не подтверждена"
        if result.is_searchable is True:
            visibility = "видно в поиске"
        elif result.is_searchable is False:
            visibility = "не видно в поиске"
        print(f"[OK] Резюме {resume.id} опубликовано")
        print(f"[INFO] Текущая видимость: {visibility}. Изменение видимости — отдельное действие.")

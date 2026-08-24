"""Команда rename-resume: переименовать резюме в списке hh.ru (#522)."""

from __future__ import annotations

import argparse

from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command
from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "rename-resume",
        help="Переименовать резюме в списке hh.ru",
        description=(
            "Изменяет название одного резюме в списке hh.ru. WRITE-hh-ru: "
            "боевой режим требует --force или интерактивного подтверждения; "
            "--dry-run ничего не сохраняет. До подтверждения селектора поля "
            "названия в живом DOM боевой режим недоступен (fail-closed)."
        ),
    )
    parser.add_argument("--resume", required=True, help="Slug из конфига или resume_id HH.ru")
    parser.add_argument("--name", required=True, help="Новое название резюме")
    parser.add_argument("--dry-run", action="store_true", help="Показать план без записи")
    parser.add_argument("--force", action="store_true", help="Подтвердить боевую запись")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..rename_resume import rename_resume_on_hh

    name = args.name.strip()
    if not name:
        print("[FAIL] Новое название резюме не может быть пустым")
        return True
    config = load_config_or_exit(args.config)
    try:
        from ._common import resolve_resume

        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True

    history = History(args.history)
    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Переименовать резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не изменено."
        )
        return True
    if not args.dry_run and history.has_unresolved_uncertain(resume.resume_id, "rename_resume"):
        print(
            f"[FAIL] {resume.id} — предыдущее переименование не подтверждено (uncertain). "
            "Проверьте название на hh.ru вручную перед повтором."
        )
        return True

    def _body(progress: ApplyProgress) -> bool:
        attempt = (
            None
            if args.dry_run
            else DurableMutationAttempt(history, progress, resume.resume_id, "rename_resume")
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                result = rename_resume_on_hh(
                    context.new_page(),
                    resume,
                    name,
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
            print(f"[DRY-RUN] Резюме {resume.id}: {result.reason}")
        else:
            print(f"[OK] Резюме {resume.id} переименовано в «{name}»")
        return False

    return run_supervised_command(
        command=getattr(args, "command", "rename-resume"),
        history=history,
        requested_limit=None,
        body=_body,
    )

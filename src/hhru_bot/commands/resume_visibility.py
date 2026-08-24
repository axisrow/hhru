"""Команда resume-visibility: изменить видимость резюме (#566)."""

from __future__ import annotations

import argparse

from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command
from .copy_resume import confirm_write

VISIBILITY_MODES = ("everyone", "no-one", "link-only", "whitelist", "blacklist")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "resume-visibility",
        help="Изменить видимость резюме на hh.ru",
        description=(
            "Изменяет видимость одного резюме. WRITE-hh.ru опасного уровня: "
            "боевой режим требует --force или подтверждения; --dry-run ничего "
            "не сохраняет. До подтверждения селектора блока видимости запись "
            "недоступна (fail-closed)."
        ),
    )
    parser.add_argument("--resume", required=True, help="Slug из конфига или resume_id HH.ru")
    parser.add_argument("--mode", required=True, choices=VISIBILITY_MODES)
    parser.add_argument("--dry-run", action="store_true", help="Показать план без записи")
    parser.add_argument("--force", action="store_true", help="Подтвердить боевую запись")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..resume_visibility import set_resume_visibility_on_hh

    config = load_config_or_exit(args.config)
    try:
        from ._common import resolve_resume

        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True

    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Изменить видимость резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не изменено."
        )
        return True

    history = History(args.history)
    action = "resume_visibility"
    if not args.dry_run and history.has_unresolved_uncertain(resume.resume_id, action):
        print(
            f"[FAIL] {resume.id} — предыдущее изменение видимости не подтверждено (uncertain). "
            "Проверьте видимость на hh.ru вручную перед повтором."
        )
        return True

    def _body(progress: ApplyProgress) -> bool:
        attempt = None if args.dry_run else DurableMutationAttempt(
            history, progress, resume.resume_id, action
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                result = set_resume_visibility_on_hh(
                    context.new_page(), resume, args.mode, args.dry_run,
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
        print(f"[DRY-RUN] Резюме {resume.id}: {result.reason}")
        if not args.dry_run:
            print(f"[OK] Резюме {resume.id}: видимость изменена на «{args.mode}»")
        return False

    return run_supervised_command(
        command=getattr(args, "command", "resume-visibility"),
        history=history,
        requested_limit=None,
        body=_body,
    )

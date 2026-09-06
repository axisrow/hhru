"""Команда wizard-next: сабмит одного экрана визарда черновика (#865/#1010).

Единственный CLI-владелец экранов educations/keyskills/experience: до неё эти
экраны приходилось кликать руками в браузере, что запрещено. Каждый боевой
клик потенциально публикует резюме (#900), поэтому --allow-auto-publish
обязателен вместе с --force; --dry-run ничего не нажимает и ничего не пишет
в history.
"""

from __future__ import annotations

import argparse
import sys


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "wizard-next",
        help="Сабмитить текущий экран визарда черновика (educations/keyskills/experience)",
        description=(
            "Кликает «Сохранить и продолжить» на незавершённом экране визарда "
            "черновика. WRITE-hh-ru: боевой режим требует --force и "
            "--allow-auto-publish — NEXT на последнем незакрытом экране hh.ru "
            "публикует резюме сам (#900); --dry-run ничего не нажимает."
        ),
    )
    p.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    p.add_argument(
        "--screen",
        choices=("educations", "keyskills", "experience"),
        default=None,
        help="Явный экран; по умолчанию — текущий nextIncompleteScreenId",
    )
    p.add_argument("--dry-run", action="store_true", help="Проверить экран без клика")
    p.add_argument("--force", action="store_true", help="Разрешить боевой UI-клик")
    p.add_argument(
        "--allow-auto-publish",
        action="store_true",
        help=(
            "Разрешить сабмит экрана, после которого hh.ru может автоматически "
            "опубликовать резюме (#900)"
        ),
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace):
    from .. import resume_wizard
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..resume_state import is_published
    from ._common import DurableMutationAttempt, resolve_resume

    config = load_config_or_exit(args.config)
    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
    if not args.dry_run and not args.force:
        print("[FAIL] Боевой режим требует --force. Ничего не нажато.")
        sys.exit(1)
    if not args.dry_run and not args.allow_auto_publish:
        print(
            "[FAIL] NEXT визарда может оказаться последним незакрытым экраном: "
            "hh.ru опубликует резюме сам (#900). Ничего не нажато. Для явного "
            "разрешения используйте --allow-auto-publish вместе с --force."
        )
        sys.exit(1)

    history = History(args.history)
    if not args.dry_run and history.has_unresolved_uncertain(resume.resume_id, "wizard_next"):
        print(
            f"[FAIL] {resume.id} — предыдущий сабмит wizard-экрана не подтверждён "
            "(uncertain). Проверьте состояние визарда перед повтором: "
            f"hhru publish-resume --resume {resume.id} --dry-run"
        )
        sys.exit(1)

    def _body(progress) -> bool:
        attempt = (
            None
            if args.dry_run
            else DurableMutationAttempt(history, progress, resume.resume_id, "wizard_next")
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                page = context.new_page()
                state = resume_wizard.read_resume_state(page, resume.resume_id)
                try:
                    target = resume_wizard.resolve_target_screen(state, args.screen)
                except resume_wizard.WizardScreenRefused as exc:
                    print(f"[FAIL] {resume.id} — {exc}")
                    return True
                print(f"[INFO] Незавершённый экран визарда: {target}")
                if args.dry_run:
                    label = resume_wizard.inspect_wizard_screen(page, resume.resume_id, target)
                    print(f"[DRY-RUN] Экран «{target}» открыт, «{label}» на месте; клика не было")
                    return False
                result = resume_wizard.submit_wizard_screen(
                    page, resume, target, before_click=attempt.before_click
                )
        except resume_wizard.WizardScreenRefused as exc:
            print(f"[FAIL] {resume.id} — {exc}")
            return True
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

        print(f"[OK] Экран «{result.screen}» подтверждён")
        # #978: attempt финализирован ДО диагностического readback'а —
        # сбой чтения не превращает подтверждённый сабмит в uncertain.
        try:
            after = resume_wizard.read_resume_state(page, resume.resume_id)
        except Exception as exc:  # noqa: BLE001 — вердикт диагностический
            print(f"[INFO] Контрольное чтение состояния не удалось: {exc}")
            return False
        if is_published(after):
            print("[INFO] hh.ru опубликовал резюме на этом экране (автопубликация #900)")
            print("[NEXT] 1. Статус в списке: hhru list-resumes")
        elif after.next_incomplete_screen_id:
            print(f"[INFO] Следующий незавершённый экран: {after.next_incomplete_screen_id}")
            print(
                f"[NEXT] 1. Следующий экран: hhru wizard-next --resume {resume.id} "
                "--allow-auto-publish --force"
            )
            print(
                f"[NEXT] 2. Проверка без клика: hhru publish-resume --resume {resume.id} --dry-run"
            )
        else:
            print("[INFO] Незавершённых экранов больше нет — черновик готов к публикации")
            print(f"[NEXT] hhru publish-resume --resume {resume.id} --dry-run")
        return False

    from ._common import run_supervised_command

    return run_supervised_command(
        command=getattr(args, "command", "wizard-next"),
        history=history,
        requested_limit=None,
        body=_body,
    )

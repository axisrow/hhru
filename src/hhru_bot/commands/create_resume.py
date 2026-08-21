"""Команда create-resume: создать пустой черновик резюме на hh.ru (#304)."""

from __future__ import annotations

import argparse

from ._audit import action_status, record_resume_action
from ._common import ApplyProgress, run_supervised_command
from .copy_resume import confirm_write, format_config_snippet


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "create-resume",
        help="Создать пустой черновик резюме на hh.ru",
        description=(
            "Открывает визард hh.ru и создаёт новый черновик резюме. "
            "Один запуск создаёт одно резюме с одной основной профессией; "
            "для нескольких профессий нужны отдельные резюме и отдельные запуски. "
            "WRITE-команда: по умолчанию только dry-run; боевой запуск требует "
            "--force или интерактивного подтверждения."
        ),
    )
    p.add_argument("--area", required=True, help="Точная leaf-профессия из live-каталога hh.ru")
    p.add_argument("--title", required=True, help="Одна основная профессия резюме")
    p.add_argument("--dry-run", action="store_true", help="Показать план без создания")
    p.add_argument("--force", action="store_true", help="Подтвердить боевое создание")
    p.set_defaults(func=run)


def run(args: argparse.Namespace):
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
    # #464 cycle-review (Codex): a blind retry after an unresolved uncertain
    # creation could add a second resume -- unlike publish/copy, create-resume
    # has no vacancy/resume_id of its own before hh.ru assigns one, so the
    # guard/marker use the fixed "account" key already shared with the
    # actions rows written below (same convention as record_action calls).
    if not dry_run and history.has_unresolved_uncertain("account", "create_resume"):
        print(
            "[FAIL] предыдущее создание резюме не подтверждено (uncertain). "
            "Проверьте список резюме на hh.ru вручную перед повтором."
        )
        raise SystemExit(1)

    def _body(progress: ApplyProgress) -> bool:
        try:
            if not dry_run:
                progress.begin_attempt()
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                result = create_resume_on_hh(
                    context.new_page(), area=args.area, title=args.title, dry_run=dry_run
                )
        except BaseException as exc:
            # A dry-run never changes hh.ru and therefore must not create an
            # action-history row, including for a local/browser failure.
            # #464 cycle-review (Codex): ``BaseException``, not ``Exception`` --
            # ``KeyboardInterrupt``/``SignalTermination`` (both BaseException,
            # not Exception, #462's own rationale) can land at any point inside
            # ``create_resume_on_hh``, including right after the browser click
            # that already created the resume on hh.ru but before the function
            # returns. A plain ``except Exception`` would let such a signal
            # skip this whole block and leave no actions row at all, so a
            # later blind retry could create a duplicate resume. There is no
            # way to tell from here whether the click already fired, so a
            # signal interrupt is recorded ``uncertain`` (fail-closed, blocks
            # a retry via has_unresolved_uncertain above) while an ordinary
            # exception (browser/network failure, never reaching the click)
            # keeps the prior ``failed`` status.
            if not dry_run:
                progress.failed_count += 1
                status = "failed" if isinstance(exc, Exception) else "uncertain"
                record_resume_action(
                    history, "account", "create_resume", status, f"исключение: {exc}"
                )
            raise
        if not dry_run:
            progress.finish(result)
            status = action_status(
                dry_run=False, success=result.success, uncertain=result.uncertain
            )
            history.record_action("account", "account", "create_resume", status, result.reason)
        if not result.success:
            prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
            print(f"{prefix} {result.reason}")
            return True
        if dry_run:
            print(f"[DRY-RUN] Создание резюме: area={args.area}, title={args.title}")
            print(f"[INFO] {result.reason}")
        else:
            print(f"[OK] Черновик резюме создан. Новый resume_id: {result.new_resume_id}")
            print(format_config_snippet(result.new_resume_id))
        return False

    return run_supervised_command(
        command=getattr(args, "command", "create-resume"),
        history=history,
        requested_limit=None,
        body=_body,
    )

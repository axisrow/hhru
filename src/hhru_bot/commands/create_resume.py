"""Команда create-resume: создать пустой черновик резюме на hh.ru (#304)."""

from __future__ import annotations

import argparse

from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command
from .copy_resume import confirm_write, format_config_snippet


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "create-resume",
        help="Создать пустой черновик резюме на hh.ru",
        description=(
            "Открывает визард hh.ru и создаёт новый черновик резюме. "
            "Один запуск создаёт одно резюме с одной основной профессией; "
            "для нескольких профессий нужны отдельные резюме и отдельные запуски. "
            "С --allow-unresolved-area можно явно создать подтверждённый черновик "
            "без профессии, если area отсутствует в каталоге. "
            "WRITE-команда: по умолчанию только dry-run; боевой запуск требует "
            "--force или интерактивного подтверждения."
        ),
    )
    p.add_argument(
        "--area",
        required=True,
        help=(
            "Профессия для выбора в визарде создания резюме. Если hh.ru "
            "показывает ровно одну подсказку автодополнения с однозначной "
            "ролью, она принимается автоматически; иначе выбирается дерево "
            "каталога — точным совпадением, а при его отсутствии единственным "
            "кандидатом фильтра (#920). Несколько подсказок автоматически не "
            "выбираются — перезапустите с точным именем одной из них"
        ),
    )
    p.add_argument("--title", required=True, help="Одна основная профессия резюме")
    p.add_argument(
        "--allow-unresolved-area",
        "--allow-unresolved",
        action="store_true",
        help=(
            "Разрешить черновик без профессии, если area не найдена в каталоге "
            "(синоним: --allow-unresolved)"
        ),
    )
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
        attempt = (
            None
            if dry_run
            else DurableMutationAttempt(history, progress, "account", "create_resume")
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                create_kwargs = {
                    "area": args.area,
                    "title": args.title,
                    "dry_run": dry_run,
                    "before_click": attempt.before_click if attempt is not None else None,
                }
                if getattr(args, "allow_unresolved_area", False):
                    create_kwargs["allow_unresolved_area"] = True
                result = create_resume_on_hh(context.new_page(), **create_kwargs)
        except BaseException as exc:
            if attempt is not None:
                attempt.interrupt(exc)
            raise
        if attempt is not None:
            attempt.finish(result)
        if not result.success:
            prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
            print(f"{prefix} {result.reason}")
            return True
        if dry_run:
            print(f"[DRY-RUN] Создание резюме: area={args.area}, title={args.title}")
            print(f"[INFO] {result.reason}")
        else:
            print(f"[OK] {result.reason}. Новый resume_id: {result.new_resume_id}")
            print(format_config_snippet(result.new_resume_id))
        return False

    return run_supervised_command(
        command=getattr(args, "command", "create-resume"),
        history=history,
        requested_limit=None,
        body=_body,
    )

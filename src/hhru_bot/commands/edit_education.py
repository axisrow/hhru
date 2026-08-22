"""CLI command for LLM-assisted education editing (#262)."""

from __future__ import annotations

import argparse

from ..resume_education import EducationPlan, generate_education_plan
from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "edit-education",
        help="Заполнить основное и/или дополнительное образование резюме",
        description=(
            "Составляет LLM-план образования и заполняет поля через UI hh.ru. "
            "--dry-run не нажимает Save; боевой режим требует --force или TTY-подтверждение."
        ),
    )
    parser.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    parser.add_argument(
        "--section",
        choices=("primary", "additional", "both"),
        default="both",
        help="Какой блок редактировать (по умолчанию: оба)",
    )
    parser.add_argument("--source", help="Контекст кандидата (переопределяет education.source)")
    parser.add_argument("--mode", choices=("from_scratch", "prefill"), help="Режим планирования")
    # Ручной ввод (#326): готовые записи без LLM; секция education в конфиге
    # и секция ai не требуются.
    parser.add_argument("--institution", help="Учебное заведение (основное образование, без LLM)")
    parser.add_argument("--faculty", help="Факультет (основное образование, без LLM)")
    parser.add_argument("--specialty", help="Специальность (без LLM)")
    parser.add_argument("--year", help="Год окончания (без LLM)")
    parser.add_argument(
        "--primary-entry",
        action="append",
        metavar="JSON",
        help=(
            "Готовая запись основного образования JSON без LLM (#326), можно несколько: "
            '\'{"institution":..., "faculty":..., "specialty":..., "year":...}\''
        ),
    )
    parser.add_argument(
        "--additional-entry",
        action="append",
        metavar="JSON",
        help=(
            "Готовая запись доп. образования JSON без LLM (#326), можно несколько: "
            '\'{"institution":..., "organization":..., "specialty":..., "year":...}\''
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Заполнить только локальную форму; Save не нажимать"
    )
    parser.add_argument("--force", action="store_true", help="Разрешить боевое сохранение")
    parser.set_defaults(func=run)


def _parse_manual_records(flag: str, raw_entries) -> list:
    """Parse repeatable JSON flags into EducationRecord; fail closed (#326)."""
    import json

    from ..resume_education import _record

    records = []
    for raw in raw_entries:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{flag} должен содержать валидный JSON: {exc}") from exc
        record = _record(item)
        if not record.institution.strip():
            raise ValueError(f"{flag} требует непустой institution")
        records.append(record)
    return records


def _run(args: argparse.Namespace, progress) -> bool:
    from ..browser import BrowserLaunchError, launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..responses import NotAuthenticated
    from ..resume_education import _record, edit_education_on_hh

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    scalar_fields = tuple(
        getattr(args, name, None) for name in ("institution", "faculty", "specialty", "year")
    )
    manual = any(value is not None for value in scalar_fields) or bool(
        getattr(args, "primary_entry", None) or getattr(args, "additional_entry", None)
    )
    if manual and (args.source is not None or args.mode is not None):
        print(
            "[FAIL] --source/--mode относятся к LLM-планированию и не сочетаются "
            "с ручными записями (#326)"
        )
        return True

    # needs='education': команда не имеет смысла без секции — точечная ошибка
    # вместо «резюме не найдено в конфиге» (#319). Ручной ввод (#326) полный,
    # секция не нужна.
    try:
        resume = resolve_resume(config, args.resume, needs=() if manual else ("education",))
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True

    education = resume.education

    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Сохранить образование резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не сохранено."
        )
        return True

    try:
        primary_manual = _parse_manual_records("--primary-entry", args.primary_entry or [])
        additional_manual = _parse_manual_records("--additional-entry", args.additional_entry or [])
        if any(value is not None for value in scalar_fields):
            scalar_record = _record(
                {
                    name: (getattr(args, name) or "")
                    for name in ("institution", "faculty", "specialty", "year")
                }
            )
            if not scalar_record.institution.strip():
                print("[FAIL] Явные поля основного образования требуют непустой --institution")
                return True
            primary_manual.append(scalar_record)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return True

    if manual:
        plan = EducationPlan(
            primary=primary_manual,
            additional=additional_manual,
            mode="manual",
        )
    else:
        source = args.source if args.source is not None else education.source
        mode = args.mode or education.mode
        llm = None
        if config.ai is not None:
            try:
                from ..ai.llm_client import LLMClient

                llm = LLMClient(config.ai)
            except ImportError as exc:
                print(f"[INFO] LLM недоступен: {exc}; используется исходный план")
        if llm is None:
            plan = EducationPlan(
                primary=list(education.primary),
                additional=list(education.additional),
                mode=mode,
                used_fallback=True,
                reason="LLM не настроен; использованы записи из education",
            )
        else:
            plan = generate_education_plan(
                llm,
                source,
                mode=mode,
                current_primary=education.primary,
                current_additional=education.additional,
            )

    plan_prefix = "[DRY-RUN]" if args.dry_run else "[INFO]"
    print(
        f"{plan_prefix} План образования: primary={len(plan.primary)}, "
        f"additional={len(plan.additional)}"
    )
    if plan.used_fallback:
        print(f"[INFO] {plan.reason}")
    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            # begin_attempt() right before the real mutation, after the
            # browser/context are already open (#465 review): counting the
            # attempt before launch_context succeeded would misreport a
            # browser-launch or auth failure as a real (but failed) attempt.
            if not args.dry_run:
                progress.begin_attempt()
            results = edit_education_on_hh(
                context.new_page(),
                resume.resume_url,
                plan,
                section=args.section,
                dry_run=args.dry_run,
            )
    except NotAuthenticated as exc:
        if not args.dry_run and progress.attempted_count:
            progress.finish(exc)
        print(f"[FAIL] {resume.id} — Сессия недействительна: {exc}")
        return True
    except BrowserLaunchError:
        # #465 review round 3: a broad `except Exception` here would swallow
        # BrowserLaunchError before it reaches cli.py's dedicated handler
        # (prints "[ENVIRONMENT] ..." and exits distinctly from an ordinary
        # command failure) — re-raise so that classification is preserved.
        raise
    except Exception as exc:  # browser/auth errors are a failed command, not a traceback
        if not args.dry_run and progress.attempted_count:
            progress.finish(exc)
        print(f"[FAIL] {resume.id} — {exc}")
        return True

    for result in results:
        prefix = "[OK]" if result.success else "[FAIL]"
        if result.uncertain:
            prefix = "[FAIL] (uncertain)"
        print(f"{prefix} {result.kind}: {result.reason}")

    if not args.dry_run:
        from ..history import History

        history = History(args.history)
        # A single classification for the whole attempt (#477): status comes
        # from ApplyProgress.finish()'s priority (skipped > uncertain > success
        # > failed) over the *entire* results batch, not from each item
        # separately re-deriving "uncertain or not success" — that would
        # classify the same attempt in two places.
        status = progress.finish(results)
        assert status is not None
        if any(result.success or result.uncertain or result.saved for result in results):
            history.record_action(
                resume.resume_id,
                resume.resume_id,
                "edit_education",
                status,
                "; ".join(result.reason for result in results),
                run_id=progress.run_id,
            )

    failed = any(not result.success for result in results)
    if failed:
        return True
    if args.dry_run:
        print("[DRY-RUN] Ничего не сохранено на hh.ru")
    else:
        print("[OK] Образование сохранено на hh.ru")
    return False


def run(args: argparse.Namespace):
    """Execute one resume-edit command under the durable command-run ledger."""
    from ._common import run_single_mutation_command

    return run_single_mutation_command(command="edit_education", args=args, body=_run)

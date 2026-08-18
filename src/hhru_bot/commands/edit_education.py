"""CLI command for LLM-assisted education editing (#262)."""

from __future__ import annotations

import argparse
import sys

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
    parser.add_argument("--resume", required=True, help="ID резюме из конфига")
    parser.add_argument(
        "--section",
        choices=("primary", "additional", "both"),
        default="both",
        help="Какой блок редактировать (по умолчанию: оба)",
    )
    parser.add_argument("--source", help="Контекст кандидата (переопределяет education.source)")
    parser.add_argument("--mode", choices=("from_scratch", "prefill"), help="Режим планирования")
    parser.add_argument(
        "--dry-run", action="store_true", help="Заполнить только локальную форму; Save не нажимать"
    )
    parser.add_argument("--force", action="store_true", help="Разрешить боевое сохранение")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..responses import NotAuthenticated
    from ..resume_education import edit_education_on_hh

    config = load_config_or_exit(args.config)
    try:
        resume = config.get_resume(args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)

    education = getattr(resume, "education", None)
    if education is None:
        print("[FAIL] Добавьте секцию education в конфиг резюме")
        sys.exit(1)
    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Сохранить образование резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не сохранено."
        )
        sys.exit(1)

    source = args.source if args.source is not None else education.source
    mode = args.mode or education.mode
    llm = None
    if getattr(config, "ai", None) is not None:
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
            results = edit_education_on_hh(
                context.new_page(),
                resume.resume_url,
                plan,
                section=args.section,
                dry_run=args.dry_run,
            )
    except NotAuthenticated as exc:
        print(f"[FAIL] {resume.id} — Сессия недействительна: {exc}")
        sys.exit(1)

    if not args.dry_run:
        from ..history import History

        history = History(args.history)
        for result in results:
            if result.success or result.uncertain:
                history.record_action(
                    resume.resume_id,
                    resume.resume_id,
                    "edit_education",
                    "uncertain" if result.uncertain else "success",
                    result.reason,
                )

    failed = [result for result in results if not result.success]
    for result in results:
        prefix = "[OK]" if result.success else "[FAIL]"
        if result.uncertain:
            prefix = "[FAIL] (uncertain)"
        print(f"{prefix} {result.kind}: {result.reason}")
    if failed:
        sys.exit(1)
    if args.dry_run:
        print("[DRY-RUN] Ничего не сохранено на hh.ru")
    else:
        print("[OK] Образование сохранено на hh.ru")

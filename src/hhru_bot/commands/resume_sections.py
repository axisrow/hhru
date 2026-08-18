"""CLI command for LLM-assisted additional resume sections (#266)."""

from __future__ import annotations

import argparse
import sys

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "resume-sections",
        help="Заполнить дополнительные разделы резюме через LLM",
        description=(
            "Заполняет только подтвержденные read-only разведкой блоки "
            "аттестаций и рекомендаций. Сертификаты, портфолио и ссылки пока пропускаются."
        ),
    )
    parser.add_argument("--resume", required=True, help="ID резюме из конфига")
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать план без изменений на hh.ru"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить WRITE без prompt")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..ai.llm_client import LLMClient
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..resume_sections import apply_plan, generate_plan

    config = load_config_or_exit(args.config)
    try:
        resume = config.get_resume(args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
    sections = getattr(resume, "resume_sections", None)
    if sections is None:
        print("[FAIL] Добавьте resume_sections в конфиг резюме")
        sys.exit(1)
    if config.ai is None or resume.ai_profile is None:
        print("[FAIL] Для resume-sections нужны секции ai и ai_profile")
        sys.exit(1)
    if not args.dry_run and not confirm_write(
        args.force,
        prompt=f"Заполнить дополнительные разделы резюме '{resume.id}' на hh.ru?",
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного "
            "подтверждения. Ничего не отправлено."
        )
        sys.exit(1)
    try:
        client = LLMClient(config.ai)
    except ImportError as exc:
        print(f"[FAIL] LLM недоступен: {exc}")
        sys.exit(1)
    plan = generate_plan(client, sections, resume.ai_profile)
    print(
        f"[{'DRY-RUN' if args.dry_run else 'INFO'}] "
        f"Аттестаций: {len(plan.attestations)}, рекомендаций: {len(plan.recommendations)}"
    )
    if args.dry_run:
        print("[INFO] Ничего не отправлено.")
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        errors = apply_plan(context.new_page(), resume.resume_id, plan, dry_run=args.dry_run)
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        sys.exit(1)
    print(
        "[OK] Дополнительные разделы обработаны." if not args.dry_run else "[INFO] План корректен."
    )

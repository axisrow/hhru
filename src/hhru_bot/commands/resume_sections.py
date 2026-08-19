"""CLI command for LLM-assisted additional resume sections (#266)."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, cast

from .copy_resume import confirm_write

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "resume-sections",
        help="Заполнить дополнительные разделы резюме через LLM",
        description=(
            "Заполняет только подтвержденные read-only разведкой блоки "
            "аттестаций и рекомендаций. Сертификаты, портфолио и ссылки пока пропускаются."
        ),
    )
    parser.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    # Ручной ввод (#326): готовые записи без LLM; ai_profile/секция ai не нужны.
    parser.add_argument(
        "--attestation",
        action="append",
        metavar="JSON",
        help=(
            "Готовая аттестация JSON без LLM (#326), можно несколько: "
            "'{\"name\":..., \"organization\":..., \"specialty\":..., \"year\":...}'"
        ),
    )
    parser.add_argument(
        "--recommendation",
        action="append",
        metavar="JSON",
        help=(
            "Готовая рекомендация JSON без LLM (#326), можно несколько: "
            "'{\"text\":..., \"company\":..., \"name\":..., \"position\":...}'"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать план без изменений на hh.ru"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить WRITE без prompt")
    parser.set_defaults(func=run)


def _parse_manual_sections(args: argparse.Namespace):
    """Parse --attestation/--recommendation JSON flags into a plan (#326)."""
    import json

    from ..resume_sections import Attestation, Recommendation, ResumeSectionsPlan, _text

    def records(flag: str, raw_items, fields, build):
        result = []
        for raw in raw_items or []:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{flag} должен содержать валидный JSON: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{flag} должен содержать JSON-объект")
            record = build(*(_text(item.get(key)) for key in fields))
            if not any(record.__dict__.values()):
                raise ValueError(f"{flag} содержит пустую запись")
            result.append(record)
        return result

    attestations = records(
        "--attestation",
        args.attestation,
        ("name", "organization", "specialty", "year"),
        Attestation,
    )
    recommendations = records(
        "--recommendation",
        args.recommendation,
        ("text", "company", "name", "position"),
        Recommendation,
    )
    if not attestations and not recommendations:
        raise ValueError("укажите хотя бы один --attestation или --recommendation")
    return ResumeSectionsPlan(attestations=attestations, recommendations=recommendations)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..resume_sections import apply_plan, generate_plan

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    manual = bool(getattr(args, "attestation", None) or getattr(args, "recommendation", None))

    # needs: точечная ошибка вместо «резюме не найдено в конфиге» (#319).
    try:
        resume = resolve_resume(
            config,
            args.resume,
            needs=() if manual else ("resume_sections", "ai_profile"),
        )
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
    if not manual and config.ai is None:
        print("[FAIL] Для resume-sections нужна секция ai в config.yaml")
        sys.exit(1)

    sections = resume.resume_sections
    # See commands/about.py for why this cast is needed: ResumeConfig.ai_profile
    # is a neutral `object | None` placeholder shared across unrelated features.
    ai_profile = cast("AIProfile", resume.ai_profile)
    if not args.dry_run and not confirm_write(
        args.force,
        prompt=f"Заполнить дополнительные разделы резюме '{resume.id}' на hh.ru?",
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного "
            "подтверждения. Ничего не отправлено."
        )
        sys.exit(1)
    if manual:
        try:
            plan = _parse_manual_sections(args)
        except ValueError as exc:
            print(f"[FAIL] {exc}")
            sys.exit(1)
    else:
        try:
            from ..ai.llm_client import LLMClient

            client = LLMClient(config.ai)
        except ImportError as exc:
            print(f"[FAIL] LLM недоступен: {exc}")
            sys.exit(1)
        plan = generate_plan(client, sections, ai_profile)
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

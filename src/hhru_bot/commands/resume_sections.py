"""CLI command for LLM-assisted additional resume sections (#266)."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, cast

from ..resume_sections import Attestation, Recommendation
from .copy_resume import confirm_write

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "resume-sections",
        help="Заполнить дополнительные разделы резюме через LLM",
        description=(
            "Заполняет аттестации и рекомендации по подтвержденным UI-маршрутам; "
            "умеет создать первую строку в пустом блоке. Сертификаты, портфолио и "
            "ссылки пока пропускаются, удаления не выполняются."
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
            '\'{"name":..., "organization":..., "specialty":..., "year":...}\''
        ),
    )
    parser.add_argument(
        "--recommendation",
        action="append",
        metavar="JSON",
        help=(
            "Готовая рекомендация JSON без LLM (#326), можно несколько: "
            '\'{"text":..., "company":..., "name":..., "position":...}\'. '
            "text не поддерживается текущей формой HH.ru и приведёт к [FAIL] "
            "для этой строки, если непустой (#367)."
        ),
    )
    # Ручные блоки #1118: схемы предварительные, финальные поля фиксирует
    # census блоковых ишью (#1119-1122). Боевой проход для них пока честно
    # завершается failed — формы не подтверждены.
    parser.add_argument(
        "--contact",
        action="append",
        metavar="JSON",
        help=(
            "Контакт JSON (#1118), можно несколько: "
            '\'{"name":..., "value":...}\' (схема предварительная, до census '
            "#1119). Боевой проход пока даёт [FAIL] для строки."
        ),
    )
    parser.add_argument(
        "--certificate",
        action="append",
        metavar="JSON",
        help=(
            "Сертификат JSON (#1118), можно несколько: "
            '\'{"name":..., "organization":..., "year":...}\' (схема '
            "предварительная, до census #1120). Боевой проход пока даёт "
            "[FAIL] для строки."
        ),
    )
    parser.add_argument(
        "--portfolio",
        action="append",
        metavar="JSON",
        help=(
            "Проект портфолио JSON (#1118), можно несколько: "
            '\'{"name":..., "url":...}\' (схема предварительная, до census '
            "#1121). Боевой проход пока даёт [FAIL] для строки."
        ),
    )
    parser.add_argument(
        "--link",
        action="append",
        metavar="JSON",
        help=(
            "Внешняя ссылка JSON (#1118), можно несколько: "
            '\'{"name":..., "url":...}\' (схема предварительная, до census '
            "#1122). Боевой проход пока даёт [FAIL] для строки."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать план без изменений на hh.ru"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить WRITE без prompt")
    parser.set_defaults(func=run)


_TYPED_BLOCK_SPECS = {
    "--attestation": ("attestations", ("name", "organization", "specialty", "year"), Attestation),
    "--recommendation": (
        "recommendations",
        ("text", "company", "name", "position"),
        Recommendation,
    ),
}
# CLI-флаг → имя блока MANUAL_BLOCK_SCHEMAS (#1118).
_MANUAL_FLAGS = {
    "--contact": "contact",
    "--certificate": "certificate",
    "--portfolio": "portfolio",
    "--link": "link",
}


def _parse_manual_sections(args: argparse.Namespace):
    """Parse manual JSON flags into a plan + per-row outcomes (#326, #1118).

    Валидация строгая (#1118): неизвестное поле — явная ошибка с перечнем
    ожидаемых полей блока, а не молчаливый пропуск. Дедуп: полное совпадение
    всех полей строки внутри блока — outcome duplicate без второй записи.
    """
    import json

    from ..resume_sections import (
        MANUAL_BLOCK_SCHEMAS,
        ManualRow,
        ResumeSectionsPlan,
        _dedupe,
        _text,
        plan_from_rows,
    )

    def parse_item(flag: str, raw: str) -> dict:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{flag} должен содержать валидный JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"{flag} должен содержать JSON-объект")
        return item

    def check_fields(flag: str, item: dict, fields: tuple[str, ...]) -> None:
        unknown = sorted(set(item) - set(fields))
        if unknown:
            raise ValueError(
                f"{flag}: неизвестные поля {', '.join(unknown)}; "
                f"ожидаемые поля: {', '.join(fields)}"
            )

    plan = ResumeSectionsPlan()
    outcomes = []
    for flag, (block, fields, builder) in _TYPED_BLOCK_SPECS.items():
        items = []
        for raw in getattr(args, flag[2:], None) or []:
            item = parse_item(flag, raw)
            check_fields(flag, item, fields)
            record = builder(*(_text(item.get(key)) for key in fields))
            if not any(record.__dict__.values()):
                raise ValueError(f"{flag} содержит пустую запись")
            items.append(record)
        kept, block_outcomes = _dedupe(block, items, lambda r: tuple(r.__dict__.values()))
        getattr(plan, block).extend(kept)
        outcomes.extend(block_outcomes)
    rows = []
    for flag, block in _MANUAL_FLAGS.items():
        fields = MANUAL_BLOCK_SCHEMAS[block]
        for raw in getattr(args, flag[2:], None) or []:
            item = parse_item(flag, raw)
            check_fields(flag, item, fields)
            values = {key: _text(item.get(key)) for key in fields}
            if not any(values.values()):
                raise ValueError(f"{flag} содержит пустую запись")
            rows.append(ManualRow(block=block, fields=values))
    if not plan.attestations and not plan.recommendations and not rows:
        raise ValueError(
            "укажите хотя бы один ручной флаг: "
            "--attestation, --recommendation, --contact, --certificate, "
            "--portfolio или --link"
        )
    if rows:
        manual_plan, manual_outcomes = plan_from_rows(rows)
        plan.manual = manual_plan.manual
        outcomes.extend(manual_outcomes)
    return plan, outcomes


def _print_outcomes(outcomes) -> int:
    """Per-row таблица исходов + число failed (#1118)."""
    from ..report import _ascii_table
    from ..resume_sections import (
        OUTCOME_APPENDED,
        OUTCOME_DUPLICATE,
        OUTCOME_FAILED,
        OUTCOME_PLANNED,
    )

    rows = [[o.block, str(o.index), o.status, o.reason] for o in outcomes]
    counts = {
        status: sum(1 for o in outcomes if o.status == status)
        for status in (OUTCOME_APPENDED, OUTCOME_PLANNED, OUTCOME_DUPLICATE, OUTCOME_FAILED)
    }
    footer = [
        "Итого",
        str(len(outcomes)),
        " ".join(f"{status}={count}" for status, count in counts.items()),
        "",
    ]
    print(_ascii_table(["Блок", "#", "Исход", "Причина"], rows, footer=footer))
    return counts[OUTCOME_FAILED]


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..resume_sections import (
        OUTCOME_FAILED,
        OUTCOME_PLANNED,
        RowOutcome,
        apply_plan,
        generate_plan,
    )

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    manual = any(getattr(args, flag[2:], None) for flag in (*_TYPED_BLOCK_SPECS, *_MANUAL_FLAGS))

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
            plan, outcomes = _parse_manual_sections(args)
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
        outcomes = None
    # Блоки #1118 без реализованной формы: боевой проход честно failed —
    # частичный успех одного блока не выдаётся за успех всего (#1118 п.3).
    if not args.dry_run:
        unsupported = [
            RowOutcome(
                o.block,
                o.index,
                OUTCOME_FAILED,
                "блок не реализован текущей формой (census — блоковые ишью "
                "#1119-1122); запись не выполнялась",
            )
            if o.status == OUTCOME_PLANNED and o.block in {b for b in _MANUAL_FLAGS.values()}
            else o
            for o in (outcomes or [])
        ]
        outcomes = unsupported if outcomes is not None else None
    print(
        f"[{'DRY-RUN' if args.dry_run else 'INFO'}] "
        f"Аттестаций: {len(plan.attestations)}, рекомендаций: {len(plan.recommendations)}, "
        f"ручных строк новых блоков: {len(plan.manual)}"
    )
    for row in plan.manual:
        fields = "; ".join(f"{key}={value}" for key, value in row.fields.items() if value)
        print(f"[INFO] {row.block}: {fields}")
    if args.dry_run:
        print("[INFO] Ничего не отправлено.")

    supported = bool(plan.attestations or plan.recommendations)
    if not supported and not args.dry_run:
        # Браузер не запускаем: писать на hh.ru нечем.
        if outcomes is not None:
            _print_outcomes(outcomes)
        print("[FAIL] Ни один блок плана не поддержан формой. Ничего не отправлено.")
        sys.exit(1)
    errors: list[str] = []
    if supported:
        outcome_map = None
        if outcomes is not None:
            outcome_map = {
                "attestations": [o for o in outcomes if o.block == "attestations"],
                "recommendations": [o for o in outcomes if o.block == "recommendations"],
            }
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            errors = apply_plan(
                context.new_page(),
                resume.resume_id,
                plan,
                dry_run=args.dry_run,
                outcomes=outcome_map,
            )
        if errors:
            for error in errors:
                prefix = "[FAIL] (uncertain)" if "uncertain" in error else "[FAIL]"
                print(f"{prefix} {error}")
        else:
            print(
                "[OK] Дополнительные разделы обработаны."
                if not args.dry_run
                else "[INFO] План корректен."
            )
    failed = 0
    if outcomes is not None:
        # Per-row контракт — честный итог: любой failed строки = неуспех команды.
        failed = _print_outcomes(outcomes)
    elif errors:
        sys.exit(1)
    if failed:
        sys.exit(1)

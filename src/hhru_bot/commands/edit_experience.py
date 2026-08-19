"""Command for proposing and optionally saving LLM-generated experience."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "edit-experience",
        help="Заполнить опыт работы с помощью LLM",
        description=(
            "Предлагает записи опыта через LLM и показывает их в dry-run. "
            "WRITE-hh-ru: боевой режим требует --force или подтверждения TTY."
        ),
    )
    parser.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    parser.add_argument("--mode", choices=("create", "fill"), default="fill")
    parser.add_argument(
        "--career",
        help="Факты карьеры для LLM (обязательно без --entry)",
    )
    parser.add_argument(
        "--entry",
        action="append",
        metavar="JSON",
        help=(
            "Готовая запись опыта JSON без LLM (#326), можно несколько: "
            '\'{"company":..., "position":..., "start_year":..., "end_year":..., '
            '"current":..., "duties":..., "achievements":[...], "company_url":...}\''
        ),
    )
    parser.add_argument(
        "--existing",
        type=Path,
        help="JSON-массив существующих записей для режима fill",
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать план, не нажимая save")
    parser.add_argument("--force", action="store_true", help="Разрешить запись без TTY prompt")
    parser.set_defaults(func=run)


def _load_existing(path: Path | None):
    if path is None:
        return []
    from ..experience import _entry

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"не удалось прочитать --existing: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("--existing должен содержать JSON-массив")
    entries = [_entry(item) for item in raw]
    if any(item is None for item in entries):
        raise ValueError("--existing содержит запись с неверной схемой")
    return entries


def _load_entries(raw_entries: list[str] | None):
    """Parse repeatable --entry JSON flags into plan entries (#326); fail closed."""
    if not raw_entries:
        return []
    from ..experience import _entry

    entries = []
    for raw in raw_entries:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--entry должен содержать валидный JSON: {exc}") from exc
        entry = _entry(item)
        if entry is None:
            raise ValueError(
                "--entry содержит запись с неверной схемой "
                "(company, position, start_year, end_year, duties, company_url — строки; "
                "achievements — строка или список строк; current — bool)"
            )
        if not entry.company.strip() or not entry.position.strip():
            raise ValueError("--entry требует непустые company и position")
        entries.append(entry)
    return entries


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..experience import (
        ExperiencePlan,
        edit_experience_on_hh,
        plan_experience,
        read_experience_on_hh,
    )
    from ..history import History
    from .copy_resume import confirm_write

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    manual = bool(getattr(args, "entry", None))
    if manual and args.career:
        print("[FAIL] --career относится к LLM-планированию и не сочетается с --entry (#326)")
        raise SystemExit(1)
    if not manual and not args.career:
        print("[FAIL] Требуется --career (LLM) или --entry (готовый текст, #326)")
        raise SystemExit(1)
    if manual and args.mode != "fill":
        print("[FAIL] --mode относится к LLM-планированию и не сочетается с --entry (#326)")
        raise SystemExit(1)

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        raise SystemExit(1) from exc
    if not manual and config.ai is None:
        print("[FAIL] AI выключен: добавьте пустую секцию 'ai' в config.yaml")
        raise SystemExit(1)
    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Сохранить опыт работы резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не сохранено."
        )
        raise SystemExit(1)
    try:
        existing = _load_existing(args.existing)
        entries = _load_entries(getattr(args, "entry", None))
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        raise SystemExit(1) from exc

    if manual:
        plan = ExperiencePlan(entries)
    else:
        # In fill mode the live form is the source of truth unless an explicit
        # fixture was supplied.  Reading uses the same UI route as writing and
        # only the confirmed cancel control; it never submits.
        if args.mode == "fill" and args.existing is None:
            try:
                with launch_context(
                    config.storage_state_file, headless=args.headless, user_agent=config.user_agent
                ) as context:
                    existing = read_experience_on_hh(context.new_page(), resume.resume_id)
            except Exception as exc:  # noqa: BLE001 - command reports browser drift clearly
                print(f"[FAIL] Не удалось прочитать существующий опыт: {exc}")
                raise SystemExit(1) from exc

        try:
            from ..ai.llm_client import LLMClient

            plan = plan_experience(
                LLMClient(config.ai), mode=args.mode, career=args.career, existing=existing
            )
        except ImportError as exc:
            print(f"[FAIL] AI-пакет не установлен: {exc}")
            raise SystemExit(1) from exc

    if plan.used_fallback:
        print(f"[INFO] {plan.reason}; безопасный fallback: существующие записи без изменений")
    print(json.dumps([entry.__dict__ for entry in plan.entries], ensure_ascii=False, indent=2))
    if args.dry_run:
        print("[DRY-RUN] save не нажат; изменений на hh.ru нет")
        return
    if plan.used_fallback or not plan.entries:
        print("[FAIL] Нет безопасного LLM-плана; боевая запись запрещена.")
        raise SystemExit(1)

    history = History(args.history)
    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            results = edit_experience_on_hh(
                context.new_page(), resume.resume_id, plan, dry_run=False
            )
    except Exception as exc:  # browser/auth errors are a failed command, not a traceback contract
        print(f"[FAIL] {resume.id} — {exc}")
        raise SystemExit(1) from exc
    success = bool(results) and all("сохранено" in item for item in results)
    history.record_action(
        resume.resume_id,
        resume.resume_id,
        "edit_experience",
        "success" if success else "failed",
        "; ".join(results),
    )
    for item in results:
        print(f"[OK] {resume.id} — {item}" if success else f"[FAIL] {resume.id} — {item}")
    if not success:
        raise SystemExit(1)

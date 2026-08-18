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
    parser.add_argument("--resume", required=True, help="ID резюме из конфига")
    parser.add_argument("--mode", choices=("create", "fill"), default="fill")
    parser.add_argument("--career", required=True, help="Факты карьеры для LLM")
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


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..experience import edit_experience_on_hh, plan_experience, read_experience_on_hh
    from ..history import History
    from .copy_resume import confirm_write

    config = load_config_or_exit(args.config)
    try:
        resume = config.get_resume(args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        raise SystemExit(1) from exc
    if config.ai is None:
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
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        raise SystemExit(1) from exc

    from ..ai.llm_client import LLMClient

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

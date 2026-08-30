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
            '\'{"company":..., "position":..., "start_year":..., "start_month":..., '
            '"end_year":..., "end_month":..., "current":..., "duties":..., '
            '"achievements":[...], "company_url":...}\'. '
            "start_month обязателен (число 1-12 строкой) — форма опыта hh.ru "
            "не сохраняется без месяца начала работы (#811)."
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
                "(company, position, start_year, start_month, end_year, end_month, "
                "duties, company_url — строки; achievements — строка или список строк; "
                "current — bool)"
            )
        if not entry.company.strip() or not entry.position.strip():
            raise ValueError("--entry требует непустые company и position")
        # #811: hh.ru отказывается сохранять форму опыта без выбранного месяца
        # начала работы (форма показывает "Пожалуйста, укажите" под полем
        # "Месяц" и save не проходит валидацию). start_year без start_month
        # раньше молча отбрасывался — теперь это гарантированный uncertain
        # ниже по пайплайну; лучше провалиться сразу с понятной причиной.
        if not entry.start_month.strip():
            raise ValueError(
                "--entry требует непустой start_month — форма опыта hh.ru не "
                "сохраняется без выбранного месяца начала работы (#811)"
            )
        entries.append(entry)
    return entries


def _run(args: argparse.Namespace, progress) -> bool:
    from ..browser import BrowserLaunchError, launch_context
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
        return True
    if not manual and not args.career:
        print("[FAIL] Требуется --career (LLM) или --entry (готовый текст, #326)")
        return True
    if manual and args.mode != "fill":
        print("[FAIL] --mode относится к LLM-планированию и не сочетается с --entry (#326)")
        return True

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True
    if not manual and config.ai is None:
        print("[FAIL] AI выключен: добавьте пустую секцию 'ai' в config.yaml")
        return True
    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Сохранить опыт работы резюме '{resume.id}' на hh.ru?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не сохранено."
        )
        return True
    try:
        existing = _load_existing(args.existing)
        entries = _load_entries(getattr(args, "entry", None))
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return True

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
                return True

        try:
            from ..ai.llm_client import LLMClient

            plan = plan_experience(
                LLMClient(config.ai), mode=args.mode, career=args.career, existing=existing
            )
        except ImportError as exc:
            print(f"[FAIL] AI-пакет не установлен: {exc}")
            return True

    if plan.used_fallback:
        print(f"[INFO] {plan.reason}; безопасный fallback: существующие записи без изменений")
    print(json.dumps([entry.__dict__ for entry in plan.entries], ensure_ascii=False, indent=2))
    if args.dry_run:
        print("[DRY-RUN] save не нажат; изменений на hh.ru нет")
        return False
    if plan.used_fallback or not plan.entries:
        print("[FAIL] Нет безопасного LLM-плана; боевая запись запрещена.")
        return True

    history = History(args.history)
    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            indexes = None
            if manual:
                # Manual entries have no protected-field merge (#327): reusing an
                # existing row's index would silently blank any field the manual
                # JSON omitted. Always append after the live row count instead.
                #
                # #815 review round 2: this used to build `indexes` as
                # range(existing_count, existing_count + N) — an arithmetic
                # GUESS at a free index, based on the same contiguous-from-0
                # assumption this PR disproves everywhere else. hh.ru's
                # edit-trigger index is an internal React counter that can
                # equal existing_count by coincidence (e.g. a resume with
                # exactly one row whose real index happens to be 1):
                # edit_experience_on_hh()'s fail-closed check only fires when
                # the guessed index is NOT among the existing ones
                # (trigger.count()==0) — a coincidental collision instead
                # silently lands on the ordinary edit-existing-row path and
                # OVERWRITES that row's content as if it were a fresh entry,
                # the exact class of data loss the fail-closed guard exists
                # to prevent for the other route. There is no reliable way
                # to predict a free index from client-side arithmetic, so
                # rather than gamble on a guess, fail closed explicitly here
                # whenever the resume already has ANY row — this is a
                # confirmed, not merely a suspected, unsafe path (see
                # resume_experience.py's module docstring for the CREATE
                # route this would otherwise fall through to).
                existing_count = len(read_experience_on_hh(page, resume.resume_id))
                if existing_count > 0:
                    print(
                        "[FAIL] --entry не поддерживает добавление записи к резюме, "
                        f"где опыт уже есть (существующих строк: {existing_count}) — "
                        "hh.ru не даёт клиенту предсказать свободный индекс новой "
                        "строки, а угаданный индекс может случайно совпасть с "
                        "существующей строкой и перезаписать её (#815)"
                    )
                    return True
                indexes = list(range(len(plan.entries)))
            # begin_attempt() right before the real mutation, after the page/
            # context are already open (#465 review): counting the attempt
            # before launch_context succeeded would misreport a browser-launch
            # or auth failure as a real (but failed) mutation attempt.
            progress.begin_attempt()
            results = edit_experience_on_hh(
                page, resume.resume_id, plan, dry_run=False, indexes=indexes
            )
    except BrowserLaunchError:
        # #465 review round 3: re-raise so cli.py's dedicated handler
        # (prints "[ENVIRONMENT] ..." and exits distinctly) still fires,
        # instead of the broad except Exception below swallowing it.
        raise
    except Exception as exc:  # browser/auth errors are a failed command, not a traceback contract
        # Only count a failure if the attempt was actually reserved (#465
        # review): read_experience_on_hh (manual re-index path) can raise
        # before begin_attempt() runs, which must not misreport attempted=0
        # as failed=1.
        if progress.attempted_count:
            progress.finish(exc)
        print(f"[FAIL] {resume.id} — {exc}")
        return True
    success = bool(results) and all(item.success for item in results)
    status = progress.finish(results)
    assert status is not None
    history.record_action(
        resume.resume_id,
        resume.resume_id,
        "edit_experience",
        status,
        "; ".join(item.reason for item in results),
        run_id=progress.run_id,
    )
    for item in results:
        prefix = "[FAIL] (uncertain)" if item.uncertain else ("[OK]" if item.success else "[FAIL]")
        print(f"{prefix} {resume.id} — {item.reason}")
    return not success


def run(args: argparse.Namespace):
    """Execute one resume-edit command under the durable command-run ledger."""
    from ._common import run_single_mutation_command

    return run_single_mutation_command(command="edit_experience", args=args, body=_run)

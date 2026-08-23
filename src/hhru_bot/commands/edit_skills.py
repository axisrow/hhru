"""Generate and safely apply key skills to a resume (#263)."""

from __future__ import annotations

import argparse
from urllib.parse import urlsplit

from ..skills import _skill_key
from .copy_resume import confirm_write


def _print_success(resume_id: str, result, *, dry_run: bool) -> None:
    """Print counts that distinguish additions from skills already present."""
    prefix = "[DRY-RUN]" if dry_run else "[OK]"
    # Match the pipeline's normalization (_skill_key): a chip rendered with a
    # double internal space or nbsp must still classify against the single-spaced
    # plan.  Bare casefold() here diverged from skills.py and reported an added
    # chip as "уже были" / "сохранить" (#536 round 3).  The raw chip spelling is
    # preserved in the report; _skill_key is only for the equality key.
    added_keys = {_skill_key(name) for name in result.added}
    # Report the chip as it was read off the resume, not as the caller spelled
    # it: matching is normalized, and #528 exists so the output confirms what
    # is actually on hh.ru.  ``existing`` is the DOM-read source of truth.
    existing_by_key = {_skill_key(name): name for name in result.existing}
    already_present = tuple(
        existing_by_key.get(_skill_key(skill.name), skill.name)
        for skill in result.proposed
        if _skill_key(skill.name) not in added_keys
    )
    # A dry run cancels the form, so nothing was added: state the counts in the
    # future tense there.  The real run keeps the wording #528 asked for.
    if dry_run:
        counts = (
            f"навыков сейчас {len(result.existing)}, "
            f"будет добавлено {len(result.added)}, "
            f"станет {len(result.existing) + len(result.added)}"
        )
    else:
        counts = (
            f"навыков было {len(result.existing)}, "
            f"добавлено {len(result.added)}, "
            f"стало {len(result.existing) + len(result.added)}"
        )
    print(f"{prefix} {resume_id}: {counts}")
    if result.added:
        label = "будут добавлены" if dry_run else "добавлены"
        print(f"  {label}: {', '.join(result.added)}")
    if already_present:
        print(f"  уже были: {', '.join(already_present)}")
    for skill in result.proposed:
        state = "добавить" if _skill_key(skill.name) in added_keys else "сохранить"
        name = existing_by_key.get(_skill_key(skill.name), skill.name)
        print(f"  - {name} [{skill.level}] — {state}")
    if dry_run:
        print("[INFO] Ничего не сохранено на hh.ru.")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "edit-skills",
        help="LLM-заполнение ключевых навыков резюме",
        description=(
            "Предлагает навыки с уровнями и, после явного подтверждения, добавляет их "
            "в inline-форму hh.ru. Без --dry-run боевой запуск требует --force "
            "или TTY-подтверждение."
        ),
    )
    parser.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    parser.add_argument("--mode", choices=("fresh", "append"), default="append")
    parser.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="NAME=LEVEL",
        help="Добавить навык вручную; LEVEL: basic, intermediate или advanced (можно повторять)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать план и отменить форму без сохранения"
    )
    parser.add_argument(
        "--force", action="store_true", help="Подтвердить WRITE без интерактивного вопроса"
    )
    parser.set_defaults(func=run)


def _run(args: argparse.Namespace, progress) -> bool:
    from ..browser import BrowserLaunchError, launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..skills import (
        build_skills_prompt,
        edit_skills_on_hh,
        parse_manual_skills,
        parse_skill_plan,
    )

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True

    if not args.dry_run and not confirm_write(
        args.force,
        prompt=f"Сохранить ключевые навыки резюме '{resume.id}' на hh.ru?",
    ):
        print("[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено.")
        return True

    try:
        manual = parse_manual_skills(args.skill)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return True

    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            # Manual values intentionally avoid an LLM call. Otherwise use the
            # same LLMClient transport and fail closed on every malformed/empty
            # response.
            if manual:
                proposed = manual
            else:
                if config.ai is None:
                    print(
                        "[FAIL] Секция ai не включена; "
                        "укажите --skill NAME=LEVEL или добавьте ai: {}"
                    )
                    return True
                from ..ai.llm_client import LLMClient
                from ..skills import read_skills

                try:
                    goto = f"https://hh.ru/resume/{resume.resume_id}"
                    from ..browser import goto_hh, has_auth_cookie, has_login_form

                    goto_hh(page, goto)
                    if not has_auth_cookie(page) or has_login_form(page):
                        raise RuntimeError("сессия hh.ru не подтверждена")
                    if urlsplit(page.url).path != f"/resume/{resume.resume_id}":
                        raise RuntimeError("страница нужного резюме не подтверждена")
                    existing = read_skills(page)
                    response = LLMClient(config.ai).chat(
                        build_skills_prompt(page.locator("body").inner_text(), existing, args.mode),
                        temperature=0,
                    )
                    if not response or not response.content:
                        raise ValueError("LLM вернул пустой ответ")
                    proposed = parse_skill_plan(response.content)
                except (ImportError, ValueError, RuntimeError) as exc:
                    print(f"[FAIL] Не удалось построить безопасный план навыков: {exc}")
                    return True

            if not args.dry_run:
                progress.begin_attempt()
            result = edit_skills_on_hh(page, resume, proposed, dry_run=args.dry_run, mode=args.mode)
    except BrowserLaunchError:
        # #465 review round 3: re-raise so cli.py's dedicated handler
        # (prints "[ENVIRONMENT] ..." and exits distinctly) still fires,
        # instead of the broad except Exception below swallowing it.
        raise
    except Exception as exc:  # browser/auth errors are a failed command, not a traceback
        if not args.dry_run and progress.attempted_count:
            progress.finish(exc)
        print(f"[FAIL] {resume.id} — {exc}")
        return True

    if not result.success:
        # acted=True and not success is the uncertain discriminator
        # (CLAUDE.md #163/#176, #465 review round 3): the click may already
        # have reached hh.ru, so this is not a definite failure.
        if not args.dry_run:
            progress.finish(result)
        prefix = "[FAIL] (uncertain)" if result.acted else "[FAIL]"
        print(f"{prefix} {resume.id} — {result.reason}")
        return True
    _print_success(resume.id, result, dry_run=args.dry_run)
    if not args.dry_run:
        progress.finish(result)
    return False


def run(args: argparse.Namespace):
    """Execute one resume-edit command under the durable command-run ledger."""
    from ._common import run_single_mutation_command

    return run_single_mutation_command(command="edit_skills", args=args, body=_run)

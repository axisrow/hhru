"""Generate and safely apply key skills to a resume (#263)."""

from __future__ import annotations

import argparse
import sys

from .copy_resume import confirm_write


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


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
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
        sys.exit(1)

    if not args.dry_run and not confirm_write(
        args.force,
        prompt=f"Сохранить ключевые навыки резюме '{resume.id}' на hh.ru?",
    ):
        print("[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено.")
        sys.exit(1)

    try:
        manual = parse_manual_skills(args.skill)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)

    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        # Manual values intentionally avoid an LLM call. Otherwise use the same
        # LLMClient transport and fail closed on every malformed/empty response.
        if manual:
            proposed = manual
        else:
            if config.ai is None:
                print(
                    "[FAIL] Секция ai не включена; укажите --skill NAME=LEVEL или добавьте ai: {}"
                )
                sys.exit(1)
            from ..ai.llm_client import LLMClient
            from ..skills import read_skills

            try:
                goto = f"https://hh.ru/resume/{resume.resume_id}"
                from ..browser import goto_hh, has_auth_cookie, has_login_form

                goto_hh(page, goto)
                if not has_auth_cookie(page) or has_login_form(page):
                    raise RuntimeError("сессия hh.ru не подтверждена")
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
                sys.exit(1)

        result = edit_skills_on_hh(page, resume, proposed, dry_run=args.dry_run, mode=args.mode)

    if not result.success:
        print(f"[FAIL] {resume.id} — {result.reason}")
        sys.exit(1)
    prefix = "[DRY-RUN]" if args.dry_run else "[OK]"
    print(f"{prefix} {resume.id}: существующие навыки сохранены: {len(result.existing)}")
    for skill in result.proposed:
        state = "добавить" if skill.name in result.added else "сохранить"
        print(f"  - {skill.name} [{skill.level}] — {state}")
    if args.dry_run:
        print("[INFO] Ничего не сохранено на hh.ru.")

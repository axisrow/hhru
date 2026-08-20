"""CLI command for the safe language/CEFR planner (#265)."""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlsplit

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "edit-languages",
        help="LLM-заполнение языков резюме с уровнями CEFR",
        description=(
            "Предлагает языки через LLM, но не угадывает уровень CEFR. "
            "Боевой режим требует явного уровня NAME=CEFR и подтверждения."
        ),
    )
    parser.add_argument(
        "--resume", required=True, help="Slug из конфига или реальный resume_id HH.ru"
    )
    parser.add_argument("--mode", choices=("fresh", "append"), default="append")
    parser.add_argument(
        "--language",
        action="append",
        default=[],
        metavar="NAME=CEFR",
        help="Добавить язык вручную; CEFR: A1, A2, B1, B2, C1 или C2 (можно повторять)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать план без записи на hh.ru")
    parser.add_argument("--force", action="store_true", help="Подтвердить WRITE без prompt")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..languages import build_languages_prompt, edit_languages_on_hh, parse_language_plan

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)

    manual = bool(args.language)
    confirmed = False
    if manual:
        from ..languages import parse_manual_languages

        try:
            proposed = parse_manual_languages(args.language)
        except ValueError as exc:
            print(f"[FAIL] {exc}")
            sys.exit(1)
        _print_plan(proposed, args.dry_run)
        if args.dry_run:
            print("[INFO] Ничего не сохранено на hh.ru.")
            return
        if not confirm_write(
            args.force,
            prompt=f"Сохранить языки резюме '{resume.id}' на hh.ru?",
        ):
            print("[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено.")
            sys.exit(1)
        confirmed = True
    else:
        if config.ai is None:
            print("[FAIL] Секция ai не включена; укажите --language NAME=CEFR или добавьте ai: {}")
            sys.exit(1)
        from ..ai.llm_client import LLMClient

        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            from ..browser import HH_BASE_URL, goto_hh, has_auth_cookie, has_login_form

            goto_hh(page, f"{HH_BASE_URL}/resume/{resume.resume_id}")
            if not has_auth_cookie(page) or has_login_form(page):
                print("[FAIL] Сессия hh.ru не подтверждена")
                sys.exit(1)
            if urlsplit(page.url).path != f"/resume/{resume.resume_id}":
                print("[FAIL] Страница нужного резюме не подтверждена")
                sys.exit(1)
            try:
                response = LLMClient(config.ai).chat(
                    build_languages_prompt(page.locator("body").inner_text(), (), args.mode),
                    temperature=0,
                )
                proposed = parse_language_plan(response.content if response else "")
            except (ImportError, ValueError, RuntimeError) as exc:
                print(f"[FAIL] Не удалось построить безопасный план языков: {exc}")
                sys.exit(1)
            _print_plan(proposed, args.dry_run)
            if args.dry_run:
                print("[INFO] Ничего не сохранено на hh.ru.")
                return
            if not confirm_write(
                args.force,
                prompt=f"Сохранить языки резюме '{resume.id}' на hh.ru?",
            ):
                print(
                    "[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено."
                )
                sys.exit(1)
            result = edit_languages_on_hh(page, resume, proposed, dry_run=False, mode=args.mode)
            _report(result, resume.id, False)
            return

    if not confirmed and not confirm_write(
        args.force,
        prompt=f"Сохранить языки резюме '{resume.id}' на hh.ru?",
    ):
        print("[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено.")
        sys.exit(1)
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        result = edit_languages_on_hh(
            context.new_page(), resume, proposed, dry_run=False, mode=args.mode
        )
    _report(result, resume.id, False)


def _print_plan(proposed, dry_run: bool) -> None:
    prefix = "[DRY-RUN]" if dry_run else "[INFO]"
    print(f"{prefix} Языков предложено: {len(proposed)}")
    for language in proposed:
        level = language.level or "нуждается в подтверждении"
        print(f"  - {language.name} [{level}]")


def _report(result, resume_id: str, dry_run: bool) -> None:
    if not result.success:
        print(f"[FAIL] {resume_id} — {result.reason}")
        sys.exit(1)
    prefix = "[DRY-RUN]" if dry_run else "[OK]"
    print(f"{prefix} {resume_id}: языков предложено: {len(result.proposed)}")
    for language in result.proposed:
        level = language.level or "нуждается в подтверждении"
        print(f"  - {language.name} [{level}]")
    if dry_run:
        print("[INFO] Ничего не сохранено на hh.ru.")

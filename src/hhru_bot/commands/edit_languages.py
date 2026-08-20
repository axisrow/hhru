"""CLI command for the safe language/CEFR planner (#265)."""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlsplit

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "edit-languages",
        help="LLM-заполнение раздела 'Языки' профиля с уровнями CEFR",
        description=(
            "Предлагает языки через LLM, но не угадывает уровень CEFR. "
            "Боевой режим требует явного уровня NAME=CEFR и подтверждения. "
            "Раздел 'Языки' общий для всего профиля hh.ru: запись применяется "
            "ко всем резюме аккаунта, а не только к --resume."
        ),
    )
    parser.add_argument(
        "--resume",
        required=True,
        help=(
            "Slug из конфига или resume_id HH.ru — используется только для "
            "выбора аккаунт-сессии; языки общие для всего профиля"
        ),
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
    from ..languages import (
        build_languages_prompt,
        edit_languages_on_hh,
        parse_language_plan,
        read_existing_languages,
        wait_for_language_card,
    )

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)

    manual = bool(args.language)
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
            prompt=(
                f"Языки — общий раздел профиля hh.ru, не части резюме '{resume.id}': "
                "запись затронет ВСЕ резюме аккаунта. Сохранить на hh.ru?"
            ),
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
        return
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

            # Languages are a profile-level entity on hh.ru, confirmed live
            # (#265): /resume/{id} never renders a languages block, only
            # /applicant/profile/me does, and a saved language applies to
            # every resume on the account.
            goto_hh(page, f"{HH_BASE_URL}/applicant/profile/me")
            if not has_auth_cookie(page) or has_login_form(page):
                print("[FAIL] Сессия hh.ru не подтверждена")
                sys.exit(1)
            if urlsplit(page.url).path != "/applicant/profile/me":
                print("[FAIL] Страница профиля не подтверждена")
                sys.exit(1)
            # #265 code-review round 1: fail closed on an indeterminate card
            # instead of silently feeding the LLM a false "no existing
            # languages" premise (PageStateIndeterminate invariant, CLAUDE.md).
            # round 2 (/review): shares wait_for_language_card/
            # read_existing_languages with edit_languages_on_hh instead of
            # duplicating the wait-then-count-then-read logic here.
            card = wait_for_language_card(page)
            if card is None:
                print("[FAIL] Карточка языков не найдена однозначно")
                sys.exit(1)
            existing = read_existing_languages(card)
            try:
                response = LLMClient(config.ai).chat(
                    build_languages_prompt(page.locator("body").inner_text(), existing, args.mode),
                    temperature=0,
                )
                content = response.content if response and response.content else ""
                proposed = parse_language_plan(content)
            except (ImportError, ValueError, RuntimeError) as exc:
                print(f"[FAIL] Не удалось построить безопасный план языков: {exc}")
                sys.exit(1)
            _print_plan(proposed, args.dry_run)
            if args.dry_run:
                print("[INFO] Ничего не сохранено на hh.ru.")
                return
            existing_keys = {value.casefold() for value in existing}
            new_without_level = [
                item.name for item in proposed if item.name.casefold() not in existing_keys
            ]
            if new_without_level:
                # LLM output never carries a level (parse_language_plan), so any
                # newly proposed language always needs a manual --language
                # value; without this early exit the command would navigate,
                # ask for write confirmation, and only then fail inside
                # edit_languages_on_hh on the first unconfirmed level.
                print(
                    "[FAIL] Уровень CEFR не подтверждён для: "
                    + ", ".join(new_without_level)
                    + ". Повторите с --language NAME=CEFR для каждого нового языка."
                )
                sys.exit(1)
            if not confirm_write(
                args.force,
                prompt=(
                    f"Языки — общий раздел профиля hh.ru, не части резюме '{resume.id}': "
                    "запись затронет ВСЕ резюме аккаунта. Сохранить на hh.ru?"
                ),
            ):
                print(
                    "[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено."
                )
                sys.exit(1)
            result = edit_languages_on_hh(page, resume, proposed, dry_run=False, mode=args.mode)
            _report(result, resume.id, False)
            return


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

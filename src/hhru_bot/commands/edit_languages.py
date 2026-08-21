"""CLI command for the safe language/CEFR planner (#265)."""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "edit-languages",
        help="LLM-заполнение раздела 'Языки' профиля с уровнями CEFR",
        description=(
            "Без --language: LLM только предлагает языки (уровень CEFR не "
            "угадывается и не пишется на hh.ru). С --language NAME=CEFR: "
            "записывает явно подтверждённые языки. Раздел 'Языки' общий для "
            "всего профиля hh.ru: запись применяется ко всем резюме "
            "аккаунта, а не только к --resume."
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать план без записи (только с --language; без него запись не идёт всегда)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Подтвердить WRITE без prompt (только с --language)",
    )
    parser.set_defaults(func=run)


def _run(args: argparse.Namespace, progress) -> bool:
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
        progress.begin_attempt()
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            result = edit_languages_on_hh(
                context.new_page(), resume, proposed, dry_run=False, mode=args.mode
            )
        if not result.success:
            progress.failed_count += 1
        else:
            progress.applied_count += 1
        _report(result, resume.id, False)
        return not result.success
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
            # round 3 (/review): both calls can raise PlaywrightError
            # (card.count(), row.locator(...).inner_text()) — catch it here
            # like every other failure path in this command, instead of
            # letting a bare traceback surface.
            try:
                card = wait_for_language_card(page)
                if card is None:
                    print("[FAIL] Карточка языков не найдена однозначно")
                    sys.exit(1)
                existing = read_existing_languages(card)
            except PlaywrightError as exc:
                print(f"[FAIL] Карточка языков не подтверждена: {exc}")
                sys.exit(1)
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
            # #265 code-review round 3: the LLM branch is a planner only, not
            # a writer. parse_language_plan guarantees every LLM-sourced
            # Language.level is None (round 1 fix), so every genuinely new
            # language always needs a manually confirmed level; there is no
            # in-call way to collect one here. A write path below this point
            # would either always be unreachable dead code (round-2 fail-fast
            # before it) or always fail inside edit_languages_on_hh on the
            # first unconfirmed level (round-1 behavior) — neither is a real
            # write path, so this branch never calls edit_languages_on_hh and
            # ignores --dry-run/--force (nothing here ever writes either way).
            # Re-run with --language NAME=CEFR (the manual branch above) to
            # actually save any newly proposed language.
            _print_plan(proposed, dry_run=True)
            print("[INFO] Ничего не сохранено на hh.ru.")
            print(
                "[INFO] Для сохранения новых языков повторите с "
                "--language NAME=CEFR для каждого языка."
            )
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


def run(args: argparse.Namespace):
    """Execute one resume-edit command under the durable command-run ledger."""
    from ..history import History
    from ._common import run_supervised_command

    history = History(getattr(args, "history", "data/history.db"))
    return run_supervised_command(
        command="edit_languages",
        history=history,
        requested_limit=1,
        body=lambda progress: _run(args, progress),
    )

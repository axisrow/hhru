from __future__ import annotations

import argparse
import logging
import sys

from .apply import apply_to_vacancy
from .browser import launch_context
from .bump import bump_resume
from .config import PROJECT_ROOT, AppConfig, ResumeConfig, load_config_or_exit
from .history import History
from .logging_setup import setup_logging
from .search import filter_candidates, search_vacancies
from .throttle import LimitReached, Throttle

logger = logging.getLogger("hhru_bot.cli")

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "data" / "history.db"


def _resolve_resumes(config: AppConfig, resume_ids: list[str] | None) -> list[ResumeConfig]:
    if not resume_ids:
        return config.resumes
    return [config.get_resume(rid) for rid in resume_ids]


def cmd_login(args: argparse.Namespace) -> None:
    from .auth import login

    config = load_config_or_exit(args.config)
    login(config)


def cmd_search(args: argparse.Namespace) -> None:
    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = _resolve_resumes(config, [args.resume] if args.resume else None)

    with launch_context(config.storage_state_file, headless=args.headless) as context:
        page = context.new_page()
        for resume in resumes:
            print(f"\n=== Поиск вакансий для резюме: {resume.id} ===")
            cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
            candidates, skipped = filter_candidates(cards, resume.search, resume.id, history)

            print(f"Найдено всего: {len(cards)}, подходящих: {len(candidates)}, исключено: {len(skipped)}")
            for c in candidates:
                print(f"  [candidate] {c.title} — {c.company} ({c.url})")
            for card, reason in skipped:
                print(f"  [skip] {card.title} — {reason}")


def cmd_apply(args: argparse.Namespace) -> None:
    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = _resolve_resumes(config, [args.resume] if args.resume else None)
    throttle = Throttle(config.throttle, history)

    with launch_context(config.storage_state_file, headless=args.headless) as context:
        page = context.new_page()
        for resume in resumes:
            _apply_for_resume(page, config, resume, history, throttle, args)


def _apply_for_resume(page, config, resume, history, throttle, args) -> None:
    print(f"\n=== Отклики для резюме: {resume.id} ===")

    try:
        throttle.check_apply_limit(resume.id, args.dry_run)
    except LimitReached as e:
        print(f"Пропуск: {e}")
        return

    cards = search_vacancies(page, resume.search, max_pages=args.max_pages)
    candidates, skipped = filter_candidates(cards, resume.search, resume.id, history)

    for card, reason in skipped:
        logger.debug("Пропуск вакансии %s: %s", card.title, reason)

    limit = args.limit if args.limit else len(candidates)
    cover_letter_template = config.cover_letter_for(resume)

    applied_count = 0
    for card in candidates[:limit]:
        try:
            throttle.check_apply_limit(resume.id, args.dry_run)
        except LimitReached as e:
            print(f"Дневной лимит достигнут, останавливаюсь: {e}")
            break

        result = apply_to_vacancy(page, card, resume.id, cover_letter_template, args.dry_run)
        status = "dry_run" if args.dry_run else ("success" if result.success else "failed")
        history.record_action(resume.id, card.vacancy_id, "apply", status, result.reason)

        if result.success:
            applied_count += 1
            print(f"  [OK] {card.title} — {card.company}")
        else:
            print(f"  [FAIL] {card.title} — {result.reason}")

        throttle.wait(f"после отклика на '{card.title}'")

    print(f"Итого откликов за этот запуск: {applied_count}")


def cmd_bump(args: argparse.Namespace) -> None:
    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = _resolve_resumes(config, [args.resume] if args.resume else None)
    throttle = Throttle(config.throttle, history)

    with launch_context(config.storage_state_file, headless=args.headless) as context:
        page = context.new_page()
        for resume in resumes:
            print(f"\n=== Поднятие резюме: {resume.id} ===")

            try:
                throttle.check_bump_limit(resume.id, args.dry_run)
            except LimitReached as e:
                print(f"Пропуск: {e}")
                continue

            can_bump, wait_left = throttle.can_bump_now(resume.id)
            if not can_bump:
                print(f"Пропуск: рано поднимать, подождите ещё {wait_left}")
                continue

            result = bump_resume(page, resume, args.dry_run)
            status = "dry_run" if args.dry_run else ("success" if result.success else "failed")
            history.record_action(resume.id, resume.resume_id, "bump", status, result.reason)

            if result.success:
                print(f"  [OK] {resume.id} поднято")
            else:
                print(f"  [FAIL] {resume.id} — {result.reason}")

            throttle.wait(f"после поднятия резюме '{resume.id}'")


def cmd_run(args: argparse.Namespace) -> None:
    print("=== Полный цикл: search -> apply -> bump ===")
    cmd_apply(args)
    cmd_bump(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hhru_bot",
        description="Автоматизация поиска, откликов и поднятия резюме на hh.ru",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="Путь к config.yaml"
    )
    parser.add_argument(
        "--history", default=str(DEFAULT_HISTORY_PATH), help="Путь к файлу истории (SQLite)"
    )
    parser.add_argument(
        "--headless", action="store_true", help="Запустить браузер в headless-режиме"
    )
    parser.add_argument("--verbose", action="store_true", help="Подробное логирование")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_login = subparsers.add_parser("login", help="Войти в аккаунт hh.ru и сохранить сессию")
    p_login.set_defaults(func=cmd_login)

    for name, func, help_text in [
        ("search", cmd_search, "Найти вакансии по фильтрам резюме (без откликов)"),
        ("apply", cmd_apply, "Найти и откликнуться на подходящие вакансии"),
        ("bump", cmd_bump, "Поднять резюме в поиске"),
        ("run", cmd_run, "Полный цикл: apply + bump для указанных резюме"),
    ]:
        p = subparsers.add_parser(name, help=help_text)
        p.add_argument("--resume", help="ID резюме из конфига (по умолчанию — все)")
        p.add_argument(
            "--dry-run", action="store_true", help="Показать, что будет сделано, без реальных действий"
        )
        p.add_argument("--max-pages", type=int, default=5, help="Максимум страниц поиска")
        if name in ("apply", "run"):
            p.add_argument("--limit", type=int, default=0, help="Максимум откликов за запуск (0 = без ограничения кроме дневного лимита)")
        p.set_defaults(func=func)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(130)


if __name__ == "__main__":
    main()

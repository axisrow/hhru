"""Отзыв откликов на hh.ru (деструктивная команда, issue #111).

Боевой режим намеренно принимает только уникальный ``topic`` или явный
``--account-wide``. Фильтры по вакансии и резюме используются лишь для плана:
страница переговоров account-scoped и не даёт надёжной атрибуции к резюме.
"""

from __future__ import annotations

import argparse
import sys

from .copy_resume import confirm_write

ACCOUNT_SCOPE = "__account__"


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "clear-negotiations",
        help="Отозвать отправленные отклики на hh.ru",
        description=(
            "Отозвать один отклик по уникальному topic или все отклики аккаунта. "
            "Фильтры --vacancy и --resume формируют только план; боевой отзыв "
            "требует --topic или --account-wide и --force/подтверждение."
        ),
    )
    p.add_argument("--topic", help="Уникальный ID переписки для точечного отзыва")
    p.add_argument("--vacancy", help="ID вакансии (только план, не боевой отзыв)")
    p.add_argument("--resume", help="ID резюме (только план, не боевой отзыв)")
    p.add_argument(
        "--account-wide",
        action="store_true",
        help="Явно отозвать все найденные отклики аккаунта",
    )
    p.add_argument("--dry-run", action="store_true", help="Показать план без отзывов")
    p.add_argument("--force", action="store_true", help="Подтвердить боевой отзыв")
    p.add_argument("--max-pages", type=int, default=5, help="Максимум страниц переговоров")
    p.set_defaults(func=run)


def _fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)


def _validate(args: argparse.Namespace) -> None:
    selectors = sum(
        bool(value) for value in (args.topic, args.account_wide, args.vacancy, args.resume)
    )
    if selectors > 1:
        _fail("Укажите только один из --topic, --vacancy, --resume или --account-wide.")
    # This is deliberately unconditional: --vacancy --force must never become
    # a write merely because the current list happens to contain one item.
    if args.vacancy and args.force:
        _fail("Боевой отзыв по --vacancy запрещён; используйте уникальный --topic.")
    if args.topic and args.dry_run:
        return
    if args.topic and not args.force and not args.dry_run:
        # The prompt is handled after validation, before opening a browser.
        return


def _withdraw_topic(page, topic: str) -> tuple[bool, str]:
    """Withdraw one negotiation through the authenticated browser context."""
    from ..browser import HH_BASE_URL

    response = page.request.delete(f"{HH_BASE_URL}/negotiations/active/{topic}")
    if response.ok:
        return True, ""
    return False, f"HTTP {response.status}"


def run(args: argparse.Namespace) -> None:
    _validate(args)

    if not args.dry_run and (args.topic or args.account_wide):
        if not confirm_write(
            args.force,
            prompt="Отозвать отклики на hh.ru? Это необратимое действие",
        ):
            _fail(
                "Боевой режим требует --force или интерактивного подтверждения. "
                "Ничего не отозвано."
            )

    if args.topic:
        if args.dry_run:
            print(f"[DRY-RUN] Отзыв отклика topic={args.topic}")
            return
        _run_topics(args, [args.topic])
        return

    # Vacancy/resume are intentionally read-only plans. With no selector there
    # is no safe target, so do not open a browser or imply that anything ran.
    if not args.account_wide:
        scope = f"vacancy={args.vacancy}" if args.vacancy else f"resume={args.resume}"
        print(f"[INFO] Только план: фильтр {scope}; боевой отзыв по нему запрещён.")
        return

    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..responses import fetch_responses
    from ..throttle import Throttle

    config = load_config_or_exit(args.config)
    history = History(args.history)
    throttle = Throttle(config.throttle, history)
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        cards = fetch_responses(page, max_pages=args.max_pages)
        topics = list(dict.fromkeys(card.topic for card in cards if card.topic))
        if args.dry_run:
            print(f"[DRY-RUN] Найдено откликов для отзыва: {len(topics)}")
            return
        _run_topics(args, topics, page=page, history=history, throttle=throttle)


def _run_topics(args, topics, *, page=None, history=None, throttle=None) -> None:
    if page is None:
        from ..browser import launch_context
        from ..config import load_config_or_exit
        from ..history import History
        from ..throttle import Throttle

        config = load_config_or_exit(args.config)
        history = History(args.history)
        throttle = Throttle(config.throttle, history)
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            _run_topics(args, topics, page=context.new_page(), history=history, throttle=throttle)
        return

    for topic in topics:
        try:
            success, reason = _withdraw_topic(page, topic)
        except Exception as exc:
            success, reason = False, str(exc)
        status = "success" if success else "failed"
        history.record_action(ACCOUNT_SCOPE, topic, "withdraw", status, reason or None)
        if success:
            print(f"[OK] Отозван отклик topic={topic}")
            throttle.wait(f"после отзыва topic={topic}")
        else:
            print(f"[FAIL] topic={topic} — {reason}")

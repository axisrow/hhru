"""Отзыв откликов на hh.ru (деструктивная команда, issue #111).

Боевой режим намеренно принимает только уникальный ``topic`` или явный
``--account-wide``. Фильтры по вакансии и резюме используются лишь для плана:
страница переговоров account-scoped и не даёт надёжной атрибуции к резюме.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..browser import HH_BASE_URL, goto_hh
from ..negotiations_probe import topic_refs
from ..responses import _has_next_page, fetch_responses
from ..selector_groups.negotiations import (
    NEGOTIATION_ITEM,
    NEGOTIATION_WITHDRAW,
    NEGOTIATION_WITHDRAW_CONFIRM,
    NEGOTIATION_WITHDRAW_SUCCESS,
)
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
    # Codex review round 3 (PR #196): range(max_pages) silently yields zero
    # iterations for max_pages<=0, so fetch_responses() would collect zero
    # pages without ever checking auth or raising an indeterminate-state
    # error — an account-wide destructive run that inspected nothing would
    # report success. Fail closed before load_config_or_exit()/opening a
    # browser is even attempted; unrelated to the discovery-ambiguity
    # trade-off (this is pure input validation, no false-refusal risk).
    if args.max_pages < 1:
        _fail(f"--max-pages должен быть >= 1 (получено {args.max_pages}).")


def _withdraw_topic(page, topic: str) -> tuple[bool, str]:
    """Withdraw one topic with an identity-bound UI click.

    This is deliberately conservative.  The topic must be present exactly once
    in the SSR state of the page opened for that topic, the SSR state must
    contain no OTHER topic (so a single remaining DOM card can't be a
    coincidence of an unfiltered list), its card and withdrawal control must
    each be unique, and success requires a positive post-click marker scoped
    to that same card.  Disappearance of a card is not evidence of success.
    """
    try:
        url = f"{HH_BASE_URL}/applicant/negotiations?topic={quote(str(topic), safe='')}"
        goto_hh(page, url)

        all_refs = topic_refs(page.content())
        refs = [ref for ref in all_refs if ref.topic_id == str(topic)]
        if len(refs) != 1:
            return False, (
                f"topic={topic} не подтверждён на открытой странице "
                f"(совпадений в SSR: {len(refs)}) — не кликаю"
            )

        # Whether ?topic= actually filters the SSR list server-side is
        # unverified (no live dump confirms it). If it doesn't, a single
        # remaining DOM card would only "prove" identity by coincidence on
        # an account with exactly one negotiation total. Any OTHER topic
        # surviving in the same SSR read means the query param did not
        # scope the page, so a single DOM card can no longer be trusted as
        # this topic's card — refuse rather than click on an unproven match.
        if len(all_refs) != 1:
            return False, (
                f"topic={topic}: страница ?topic= вернула {len(all_refs)} "
                "переговоров вместо одной — фильтр по topic не подтверждён, "
                "однозначность карточки нельзя доказать — не кликаю"
            )

        cards = page.locator(NEGOTIATION_ITEM)
        card_count = cards.count()
        if card_count != 1:
            return False, (
                f"карточка topic={topic} определяется неоднозначно "
                f"(совпадений: {card_count}) — не кликаю"
            )

        card = cards.first
        controls = card.locator(NEGOTIATION_WITHDRAW)
        control_count = controls.count()
        if control_count != 1:
            return False, (
                f"кнопка отзыва topic={topic} не подтверждена "
                f"(совпадений: {control_count}) — не кликаю"
            )

        # Any confirmation dialog already present means the page is not in a
        # uniquely actionable state for THIS click: it may belong to a stale
        # attempt (e.g. a previous topic in _run_topics' page-reuse loop) and
        # must not be mistaken for a dialog our own click is about to raise.
        confirmation = card.locator(NEGOTIATION_WITHDRAW_CONFIRM)
        confirmation_count = confirmation.count()
        if confirmation_count > 0:
            return False, (
                f"подтверждение отзыва topic={topic} уже присутствует на странице "
                f"(совпадений: {confirmation_count}) — не кликаю"
            )

        controls.first.click()

        # Some UI revisions show an explicit confirmation control after the
        # first click.  It is still a UI click and must also be unique, and
        # scoped to the same card so a stale dialog elsewhere can't be hit.
        confirmation_count = confirmation.count()
        if confirmation_count > 1:
            return False, (
                f"подтверждение отзыва topic={topic} неоднозначно "
                f"(совпадений: {confirmation_count}) — не продолжаю"
            )
        if confirmation_count == 1:
            confirmation.first.click()

        # Scoped to the identity-confirmed card: a page-wide locator could be
        # satisfied by an unrelated negotiation's marker (a prior withdrawal
        # still rendered on the page, or a stale marker from a previous
        # iteration of _run_topics' page-reuse loop), which would report
        # success for a topic that was never actually withdrawn.
        success = card.locator(NEGOTIATION_WITHDRAW_SUCCESS)
        try:
            success.first.wait_for(state="visible", timeout=10_000)
        except (PlaywrightTimeoutError, PlaywrightError):
            return False, (
                f"hh.ru не подтвердил отзыв topic={topic} позитивным маркером "
                "— не считаю операцию успешной"
            )
        if success.count() != 1:
            return False, (
                f"маркер успешного отзыва topic={topic} неоднозначен "
                f"(совпадений: {success.count()})"
            )
        return True, ""
    except (PlaywrightTimeoutError, PlaywrightError, ValueError) as exc:
        return False, f"состояние отзыва topic={topic} не подтверждено: {exc}"


def run(args: argparse.Namespace) -> bool:
    """Return True when the run failed (fail-closed contract, see cli.main)."""
    _validate(args)

    if not args.dry_run and (args.topic or args.account_wide):
        if not confirm_write(
            args.force,
            prompt="Отозвать отклики на hh.ru? Это необратимое действие",
        ):
            _fail(
                "Боевой режим требует --force или интерактивного подтверждения. Ничего не отозвано."
            )

    if args.topic:
        if args.dry_run:
            print(f"[DRY-RUN] Отзыв отклика topic={args.topic}")
            return False
        return _run_topics(args, [args.topic])

    # Vacancy/resume are intentionally read-only plans. With no selector there
    # is no safe target, so do not open a browser or imply that anything ran.
    if not args.account_wide:
        scope = f"vacancy={args.vacancy}" if args.vacancy else f"resume={args.resume}"
        print(f"[INFO] Только план: фильтр {scope}; боевой отзыв по нему запрещён.")
        return False

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
        page = context.new_page()
        cards = fetch_responses(page, max_pages=args.max_pages)

        # An empty first-page result from fetch_responses() is NOT a confirmed
        # empty inbox (Codex review round 2, PR #196): responses.py's own
        # docstring documents this as a known compromise — a render timeout on
        # page 0 is indistinguishable from a genuinely empty account and is
        # deliberately treated as empty there (no verified empty-state
        # selector exists yet). Refusing outright here would falsely block a
        # legitimately empty account on every run, so this stays a visible
        # warning rather than a hard fail-closed refusal — but it must not be
        # silent, per issue #111's "any doubt -> refuse" principle.
        if not cards:
            print(
                "[WARN] Найдено 0 откликов — hh.ru не отличает пустой аккаунт от "
                "сбоя загрузки страницы (устаревший селектор/таймаут рендера); "
                "проверьте `probe --healthcheck` при сомнении.",
                file=sys.stderr,
            )

        # Fail-closed on truncated pagination (Codex review, PR #196): fetch_responses
        # silently stops after --max-pages pages even if hh.ru has more. Withdrawing
        # only the truncated prefix and reporting success on a destructive,
        # irreversible operation would leave an unwithdrawn remainder invisible to
        # the operator. _has_next_page() is the same confirmed-pagination check
        # fetch_responses itself uses internally; re-run it against the last loaded
        # page (page is left there by fetch_responses) before withdrawing anything.
        if cards and _has_next_page(page, args.max_pages - 1):
            _fail(
                f"Достигнут лимит --max-pages={args.max_pages}, но на hh.ru есть ещё "
                "страницы откликов. Увеличьте --max-pages — иначе часть откликов "
                "не будет отозвана. Ничего не отозвано."
            )

        # Cards without a resolved topic (no chat, or ambiguous SSR match — see
        # ResponseItem.topic_ambiguous) are legitimately skipped from withdrawal,
        # but must not vanish silently: an account-wide run that reports success
        # while some negotiations were never even attempted defeats the point of
        # "account-wide" (Codex review, PR #196).
        topics = list(dict.fromkeys(card.topic for card in cards if card.topic))
        skipped = len(cards) - len(topics)

        if args.dry_run:
            print(f"[DRY-RUN] Найдено откликов для отзыва: {len(topics)}")
            if skipped:
                print(f"[INFO] Пропущено без topic (нет чата/неоднозначный SSR): {skipped}")
            return False

        if skipped:
            print(f"[INFO] Пропущено без topic (нет чата/неоднозначный SSR): {skipped}")
        # A skipped card means the account-wide run did not attempt every
        # negotiation it found. Reporting success (False) here would silently
        # understate coverage on a destructive, irreversible operation (Codex
        # review round 2, PR #196) — fold it into the same failure signal as a
        # failed withdrawal so callers/CI see an incomplete run. Withdraw first
        # (short-circuiting on `skipped` would skip resolved topics entirely).
        withdraw_failed = _run_topics(args, topics, page=page, history=history, throttle=throttle)
        return withdraw_failed or bool(skipped)


def _run_topics(args, topics, *, page=None, history=None, throttle=None) -> bool:
    """Withdraw each topic; return True if any withdrawal failed."""
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
            return _run_topics(
                args, topics, page=context.new_page(), history=history, throttle=throttle
            )

    failed = False
    for topic in topics:
        try:
            success, reason = _withdraw_topic(page, topic)
        except Exception as exc:
            success, reason = False, str(exc)
        status = "success" if success else "failed"
        # history is only ever None on the empty-discovery path (no topics
        # means the loop never runs); with real topics the callers above
        # always supply it, so it cannot be None here.
        assert history is not None
        history.record_action(ACCOUNT_SCOPE, topic, "withdraw", status, reason or None)
        if success:
            print(f"[OK] Отозван отклик topic={topic}")
            if throttle is not None:
                throttle.wait(f"после отзыва topic={topic}")
        else:
            print(f"[FAIL] topic={topic} — {reason}")
            failed = True
    return failed

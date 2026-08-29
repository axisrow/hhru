"""Команда delete-education-entry: удалить одну запись образования (#802).

Запись адресуется реальным hh.ru id (числовой хвост URL
``/profile/edit/{kind}Education/{id}``), а НЕ resume_id и НЕ индексом в
списке: профильные записи образования/опыта не привязаны к конкретному
резюме и могут быть "осиротевшими" (запись создана, резюме удалено) --
именно этот сценарий и стал мотивирующим кейсом issue #802.
"""

from __future__ import annotations

import argparse

from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command
from .copy_resume import confirm_write


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "delete-education-entry",
        help="Удалить одну запись образования по её hh.ru id",
        description=(
            "Удаляет ровно одну запись основного/дополнительного образования через UI "
            "hh.ru, адресуя её реальным id из /profile/edit/{kind}Education/{id}. "
            "--entry-id и --kind обязательны; по умолчанию выполняется только dry-run."
        ),
    )
    p.add_argument(
        "--entry-id",
        required=True,
        help="Числовой id записи из URL /profile/edit/{kind}Education/{id}",
    )
    p.add_argument(
        "--kind",
        required=True,
        choices=("primary", "additional"),
        help="Основное (primary) или дополнительное (additional) образование",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Показать план без удаления (по умолчанию; --force включает боевой режим)",
    )
    p.add_argument("--force", action="store_true", help="Подтвердить необратимое удаление")
    p.set_defaults(func=run)


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..responses import NotAuthenticated
    from ..resume_education import delete_education_entry_on_hh

    config = load_config_or_exit(args.config)
    # Fail closed: --force is the sole switch that can leave dry-run.
    dry_run = not args.force
    history = History(args.history)

    if not dry_run and not confirm_write(
        args.force,
        prompt=f"НЕОБРАТИМО удалить запись образования '{args.entry_id}' на hh.ru?",
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не удалено."
        )
        raise SystemExit(1)
    # Deliberately NO has_unresolved_uncertain pre-flight guard here, unlike
    # delete-resume (#464)/publish-resume/copy-resume. Those guards exist
    # because a blind retry there would re-click without first checking
    # whether the earlier click actually landed. Here the retry itself IS the
    # check: delete_education_entry_on_hh re-navigates to the id-scoped edit
    # route before ever considering a click, and a gone entry resolves to
    # not_found=True with no click at all (see resume_education.py's
    # EducationDeleteResult docstring for the #802-vs-#480 reasoning). Adding
    # a guard here would block the only path that ever resolves an unresolved
    # uncertain marker, recreating #480's permanent-lockout trap instead of
    # avoiding it. What DOES still need protecting is a genuinely still-open
    # entry: that path re-runs the normal DurableMutationAttempt seam below
    # and can leave a fresh 'uncertain' row exactly like the first attempt --
    # which is the correct, resolvable outcome, not a double-submit risk
    # (there is no separate submit step here to double-fire; a second click
    # on an already-deleted entry's now-absent button is caught by the
    # button.count() != 1 fail-closed check before any click happens).

    def _body(progress: ApplyProgress) -> bool:
        attempt = (
            None
            if dry_run
            else DurableMutationAttempt(history, progress, args.entry_id, "delete_education_entry")
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                result = delete_education_entry_on_hh(
                    context.new_page(),
                    args.kind,
                    args.entry_id,
                    dry_run,
                    before_click=attempt.before_click if attempt is not None else None,
                )
        except NotAuthenticated as exc:
            print(f"[FAIL] запись {args.entry_id} — Сессия недействительна: {exc}")
            return True
        except BaseException as exc:
            if attempt is not None:
                attempt.interrupt(exc)
            raise

        if attempt is not None:
            if attempt.action_id is not None:
                # The click actually fired (before_click reserved the row) --
                # the normal DurableMutationAttempt seam finalizes it.
                attempt.finish(result)
            elif result.not_found:
                # #802 vs #480: not_found (entry already gone, no click
                # needed -- first attempt on a stale id, or a retry that
                # found a prior uncertain click's target already deleted)
                # never reaches before_click, so there is no reserved row
                # for attempt.finish() to update. Record success directly so
                # a later run's has_unresolved_uncertain check for this
                # entry_id sees a resolving 'success' row and stops blocking
                # retries -- without this, a resolved retry would silently
                # never clear the uncertain marker.
                #
                # AO reviewer PR #806: progress.finish() is a silent no-op
                # unless a matching begin_attempt() ran first (it guards on
                # `_finished_attempts >= attempted_count`) -- before_click()
                # is the usual caller and this branch never reaches it, so
                # without this call command_runs stayed at attempted=0
                # success=0 for the exact retry path this PR's #480 write-up
                # claims to resolve, even though actions/stdout were correct.
                progress.begin_attempt()
                progress.finish(result)
                history.record_action(
                    args.entry_id,
                    args.entry_id,
                    "delete_education_entry",
                    "success",
                    result.reason,
                    run_id=progress.run_id,
                    reason_code="not_found",
                )
            else:
                # Every other no-click failure (button.count() != 1, an
                # ambiguous delete control) is a pre-action early exit --
                # CLAUDE.md's principle for the whole project: no trace was
                # left on hh.ru, so it is not recorded in actions, matching
                # apply/bump's "early exits before the click do not write
                # failed" convention. progress still needs begin_attempt()
                # for the same reason as the not_found branch above, so this
                # attempt is counted in command_runs (attempted/failed).
                progress.begin_attempt()
                progress.finish(result)
        if not result.success:
            prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
            print(f"{prefix} запись {args.entry_id} — {result.reason}")
            return True
        if dry_run:
            print(f"[DRY-RUN] Запись {args.entry_id}: {result.reason}")
            print("[INFO] Ничего не удалено.")
        elif result.not_found:
            print(f"[OK] Запись {args.entry_id}: {result.reason}")
        else:
            print(f"[OK] Запись {args.entry_id} удалена")
        return False

    return run_supervised_command(
        command=getattr(args, "command", "delete-education-entry"),
        history=history,
        requested_limit=None,
        body=_body,
    )

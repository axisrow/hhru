"""Команда apply: поиск и отклик на подходящие вакансии с троттлингом."""

from __future__ import annotations

import argparse
import logging
import signal

from ._common import (
    ApplyProgress,
    ApplyRunStopped,
    _build_scoring_provider,
    add_common_args,
    add_force_arg,
    add_limit_arg,
    apply_search_page_limit,
    resumes_from_args,
    run_apply_for_resume,
)

logger = logging.getLogger("hhru_bot.cli")


def register(subparsers) -> None:
    p = subparsers.add_parser("apply", help="Найти и откликнуться на подходящие вакансии")
    add_common_args(p, max_pages_default=None)
    add_force_arg(p)
    add_limit_arg(p)
    p.add_argument(
        "--approved", type=int, metavar="ID", help="Отправить ровно approved-запись review-очереди"
    )
    p.add_argument("--permit", help="Одноразовый permit из `review approve`")
    p.set_defaults(func=run)


class _SignalTermination(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def _run(args: argparse.Namespace, config, history, progress: ApplyProgress) -> bool:
    from ..browser import launch_context
    from ..throttle import LimitReached, Throttle

    if getattr(args, "approved", None) is not None and args.dry_run:
        print("[FAIL] --approved нельзя использовать вместе с --dry-run")
        return True

    if getattr(args, "approved", None) is not None and args.resume is None:
        items = [item for item in history.review_items() if item["id"] == args.approved]
        matches = [
            resume
            for resume in config.resumes
            if items and resume.resume_id == items[0]["resume_id"]
        ]
        if not matches:
            print("[FAIL] approved-запись не связана с резюме из текущего конфига")
            return True
        args.resume = matches[0].id
    resumes = resumes_from_args(config, args)
    throttle = Throttle(config.throttle, history)

    try:
        throttle.check_apply_limit(resumes[0].resume_id if resumes else "", args.dry_run)
    except LimitReached as e:
        # The apply limit is account-wide; check it before opening a browser or
        # paying for cross-resume search/scoring.
        print(f"Пропуск: {e}")
        return False

    failed = False
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        if len(resumes) > 1:
            from ..apply.antibot import raise_for_antibot
            from ..apply.router import merge_vacancies, route_vacancies
            from ..search import VacancySearchIndeterminate, search_vacancies

            routing_resumes = resumes
            if not args.dry_run:
                from ..copy_resume import resolve_numeric_resume_ids

                ids_by_hash = resolve_numeric_resume_ids(page)
                raise_for_antibot(page)
                if ids_by_hash is not None:
                    ready = {
                        resume.resume_id
                        for resume in resumes
                        if getattr(ids_by_hash, "statuses", {}).get(resume.resume_id)
                        not in (None, "not_finished")
                    }
                    routing_resumes = [r for r in resumes if r.resume_id in ready]
            feeds = []
            unconfirmed_resume_ids = set()
            for resume in routing_resumes:
                try:
                    feeds.append(
                        (
                            resume,
                            search_vacancies(
                                page,
                                resume.search,
                                max_pages=apply_search_page_limit(args),
                            ),
                        )
                    )
                except VacancySearchIndeterminate as e:
                    raise_for_antibot(page)
                    print(
                        f"[FAIL] {e}; state={e.state} page={e.page_num} url={e.url} "
                        f"partial_results={len(e.partial_results)} diagnostics={e.diagnostics}"
                    )
                    # Codex adversarial review (cycle-review PR #460, round 1):
                    # partial_results are an unconfirmed snapshot (search state
                    # itself was not verified) -- routing them into the live
                    # apply feed let this resume's applies go out against data
                    # the search step already flagged as unreliable. Fail-closed
                    # per CLAUDE.md #5: exclude this resume from routing/apply
                    # entirely for this run instead of merging partial_results in.
                    unconfirmed_resume_ids.add(resume.id)
                    failed = True
                raise_for_antibot(page)
            routing_resumes = [r for r in routing_resumes if r.id not in unconfirmed_resume_ids]
            merged = merge_vacancies(feeds)
            providers = {r.id: _build_scoring_provider(config, r) for r in routing_resumes}
            routed = route_vacancies(
                merged,
                routing_resumes,
                history,
                scoring_providers=providers,
            )
            cards_by_resume = {
                resume.id: sorted(
                    [
                        item.card
                        for item in merged
                        if routed.get(item.card.vacancy_id, None)
                        and routed[item.card.vacancy_id].resume is resume
                    ],
                    key=lambda card: -routed[card.vacancy_id].score,
                )
                for resume in resumes
            }
            ranked_by_resume = {
                resume.id: sorted(
                    [
                        (
                            item.card,
                            routed[item.card.vacancy_id].score,
                            routed[item.card.vacancy_id].breakdown,
                        )
                        for item in merged
                        if routed.get(item.card.vacancy_id, None)
                        and routed[item.card.vacancy_id].resume is resume
                    ],
                    key=lambda item: -item[1],
                )
                for resume in resumes
            }
        else:
            cards_by_resume = None
            ranked_by_resume = None
        # #441 round-2 review: --limit документирован как "целевое число
        # успешных откликов ЗА ЗАПУСК", а не за резюме — общий ApplyProgress
        # должен считать успехи по всем резюме этого прогона, иначе --limit N
        # с M резюме может дать до N*M откликов вместо N.
        try:
            for resume in resumes:
                if cards_by_resume is None:
                    result = run_apply_for_resume(
                        page, config, resume, history, throttle, args, progress=progress
                    )
                else:
                    result = run_apply_for_resume(
                        page,
                        config,
                        resume,
                        history,
                        throttle,
                        args,
                        cards_by_resume[resume.id],
                        True,
                        ranked_by_resume[resume.id],
                        progress,
                    )
                failed = result or failed
        except ApplyRunStopped:
            failed = True
    return failed


def run(args: argparse.Namespace) -> bool:
    """Run apply under a durable ledger and typed signal supervision."""
    from ..config import load_config_or_exit
    from ..exit_codes import CommandExitCode
    from ..history import History

    if getattr(args, "approved", None) is not None and args.dry_run:
        print("[FAIL] --approved нельзя использовать вместе с --dry-run")
        return True

    config = load_config_or_exit(args.config)
    history = History(args.history)
    run_id = history.start_command_run(
        command=getattr(args, "command", "apply"),
        requested_limit=getattr(args, "limit", None),
    )
    progress = ApplyProgress(run_id=run_id)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _terminate(signum, _frame):  # noqa: ANN001
        raise _SignalTermination(signum)

    signal.signal(signal.SIGTERM, _terminate)
    final_status = "failed"
    exit_code = 1
    detail = None
    result: bool | CommandExitCode = True
    try:
        failed = _run(args, config, history, progress)
        final_status = (
            "partial"
            if failed and progress.attempted_count
            else "failed"
            if failed
            else "completed"
        )
        exit_code = 1 if failed else 0
        result = bool(failed)
    except KeyboardInterrupt:
        final_status = "interrupted"
        exit_code = CommandExitCode.SIGINT.value
        detail = "SIGINT"
        result = CommandExitCode.SIGINT
    except _SignalTermination as exc:
        final_status = "interrupted"
        exit_code = 128 + exc.signum
        detail = f"signal={exc.signum}"
        result = CommandExitCode.SIGTERM
    except BaseException as exc:
        detail = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        # cycle-review PR #460 (round 3, Claude /review): this bookkeeping can
        # itself raise (e.g. history.finish_command_run's ValueError when the
        # run row is no longer 'running') while a real exception from `_run`
        # is already propagating through the `except BaseException: raise`
        # above -- an exception raised here would replace/mask it (standard
        # Python finally semantics), silently swallowing the original crash.
        # Log-and-continue instead: the ledger row staying 'running'/stale is
        # benign (`orphaned` already exists as the recognized terminal status
        # for exactly this kind of leftover, recovered on the next apply run).
        try:
            # If interruption landed inside an attempted vacancy after its
            # durable reservation, account for the unresolved result in the
            # run summary.
            action_counts = history.command_run_action_counts(run_id)
            progress.applied_count = max(progress.applied_count, action_counts.get("success", 0))
            progress.failed_count = max(progress.failed_count, action_counts.get("failed", 0))
            progress.uncertain_count = max(
                progress.uncertain_count, action_counts.get("uncertain", 0)
            )
            completed = (
                progress.applied_count
                + progress.failed_count
                + progress.uncertain_count
                + progress.skipped_count
            )
            if progress.attempted_count > completed:
                progress.failed_count += progress.attempted_count - completed
            history.finish_command_run(
                run_id,
                status=final_status,
                exit_code=exit_code,
                attempted=progress.attempted_count,
                success=progress.applied_count,
                failed=progress.failed_count,
                uncertain=progress.uncertain_count,
                skipped=progress.skipped_count,
                detail=detail,
            )
            print(progress.summary(final_status))
        except Exception:
            logger.exception(
                "apply: не удалось финализировать durable ledger run_id=%s "
                "(поглощено, чтобы не заслонить исходное исключение)",
                run_id,
            )
    return result

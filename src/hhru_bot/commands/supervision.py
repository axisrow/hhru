"""Durable supervision for WRITE-commands: progress, signal handling, mutation lease.

Вынесено из ``commands/_common.py`` (#1045, участок 2 аудита #1035). Здесь
живёт только надзор исполнения: стабильные счётчики попыток
(:class:`ApplyProgress`), durable command_run lease + SIGTERM-надзор
(:func:`run_supervised_command`) и резервирование мутации на границе клика
(:class:`DurableMutationAttempt`). Прикладной цикл откликов — в
``commands/apply_service.py``; разбор аргументов CLI — в ``commands/_common.py``.
"""

from __future__ import annotations

import argparse
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass, field

from ..browser import NotAuthenticated
from ..exit_codes import CommandExitCode
from ..history import CommandRunBusy, History

logger = logging.getLogger("hhru_bot.cli")


@dataclass
class ApplyProgress:
    """Stable per-run counters shared by all resumes/search waves."""

    applied_count: int = 0
    attempted_count: int = 0
    failed_count: int = 0
    uncertain_count: int = 0
    skipped_count: int = 0
    run_id: str | None = None
    _finished_attempts: int = field(default=0, init=False, repr=False, compare=False)

    def reached(self, limit: int | None) -> bool:
        return limit is not None and self.applied_count >= limit

    def begin_attempt(self) -> None:
        self.attempted_count += 1

    def finish(
        self,
        result,  # noqa: ANN001 - command result types intentionally share a protocol
        *,
        uncertain_exceptions: tuple[type[BaseException], ...] = (),
    ) -> str | None:
        """Classify and count the current attempt exactly once.

        Single results use structural flags (``skipped``, ``uncertain`` or
        ``acted``, then ``success``).  Batch results are first collapsed so a
        definite failure cannot be hidden by an uncertain sibling.  Typed
        post-click exceptions can be supplied through ``uncertain_exceptions``;
        exception messages are deliberately never inspected.
        """
        if self._finished_attempts >= self.attempted_count:
            return None
        self._finished_attempts += 1

        if isinstance(result, BaseException):
            status = "uncertain" if isinstance(result, uncertain_exceptions) else "failed"
        elif isinstance(result, (list, tuple)):
            status = _classify_result_batch(result)
        else:
            skipped = bool(getattr(result, "skipped", False))
            success = bool(
                getattr(result, "success", result if isinstance(result, bool) else False)
            )
            uncertain = bool(
                getattr(result, "uncertain", False)
                or (getattr(result, "acted", False) and not success)
            )
            status = (
                "skipped"
                if skipped
                else "uncertain"
                if uncertain
                else "success"
                if success
                else "failed"
            )

        if status == "skipped":
            self.skipped_count += 1
        elif status == "uncertain":
            self.uncertain_count += 1
        elif status == "success":
            self.applied_count += 1
        else:
            self.failed_count += 1
        return status

    def summary(self, status: str) -> str:
        return (
            f"[RUN] id={self.run_id or '-'} status={status} "
            f"attempted={self.attempted_count} success={self.applied_count} "
            f"failed={self.failed_count} uncertain={self.uncertain_count} "
            f"skipped={self.skipped_count}"
        )


def _classify_result_batch(results: list | tuple) -> str:
    """Collapse a batch into the single status consumed by ``finish``."""
    if not results:
        return "failed"
    flags = []
    for result in results:
        success = bool(getattr(result, "success", False))
        skipped = bool(getattr(result, "skipped", False))
        uncertain = bool(
            getattr(result, "uncertain", False) or (getattr(result, "acted", False) and not success)
        )
        flags.append((skipped, uncertain, success))
    hard_failed = any(
        not skipped and not uncertain and not success for skipped, uncertain, success in flags
    )
    if hard_failed:
        return "failed"
    if all(skipped for skipped, _uncertain, _success in flags):
        return "skipped"
    if any(uncertain for _skipped, uncertain, _success in flags):
        return "uncertain"
    if all(success for _skipped, _uncertain, success in flags):
        return "success"
    return "failed"


@dataclass(frozen=True)
class MutationOutcome:
    """Minimal structural result for mutations that otherwise return no object."""

    success: bool = False
    uncertain: bool = False
    skipped: bool = False


@dataclass
class DurableMutationAttempt:
    """Reserve/finalize one resume mutation at its browser click boundary."""

    history: History
    progress: ApplyProgress
    resume_id: str
    action: str
    action_id: int | None = None

    def before_click(self) -> None:
        if self.action_id is not None:
            raise RuntimeError(f"{self.action}: durable intent уже зарезервирован")
        self.action_id = self.history.begin_action(
            self.resume_id,
            self.resume_id,
            self.action,
            run_id=self.progress.run_id,
        )
        self.progress.begin_attempt()

    def finish(self, result) -> None:  # noqa: ANN001 - shared structural result protocol
        if self.action_id is None:
            return
        status = self.progress.finish(result)
        if status is None:
            raise RuntimeError(f"{self.action}: попытка уже финализирована")
        self.history.finalize_action(
            self.action_id,
            status,
            getattr(result, "reason", None),
            reason_code=status,
        )
        # #978 (ревью PR #980): после финализации попытка закрыта навсегда —
        # исключение в диагностическом readback, который вызывающий код
        # выполняет ПОСЛЕ finish, не должно попадать в interrupt() и
        # перезаписывать доказанный успех как uncertain.
        self.action_id = None

    def interrupt(self, exc: BaseException) -> None:
        if self.action_id is None:
            return
        outcome = MutationOutcome(uncertain=True)
        self.progress.finish(outcome)
        self.history.finalize_action(
            self.action_id,
            "uncertain",
            f"исключение после точки невозврата: {type(exc).__name__}: {exc}",
            reason_code="uncertain",
        )


class SignalTermination(BaseException):
    """Raised from the SIGTERM handler installed by ``run_supervised_command``.

    ``BaseException`` (not ``Exception``, #462 advisor review): several
    ``except BaseException`` guards in the apply pipeline (and the command's
    own ``finally``-based ledger bookkeeping below) must see this the same
    way they see ``KeyboardInterrupt`` -- a plain ``Exception`` subclass
    would let it be silently absorbed by a broad ``except Exception`` layer
    somewhere in the pipeline instead of propagating up to this supervisor.
    """

    def __init__(self, signum: int):
        self.signum = signum


def run_supervised_command(
    *,
    command: str,
    history: History,
    requested_limit: int | None,
    body: Callable[[ApplyProgress], bool | CommandExitCode],
    reconcile: Callable[[ApplyProgress, History, str], None] | None = None,
    print_summary: bool = True,
) -> bool | CommandExitCode:
    """Run ``body`` under a durable command_run ledger row + typed signal supervision.

    Extracted from ``commands/apply.py`` (#462, second sub-issue of #459) so
    other WRITE-hh.ru commands can reuse the same SIGINT/SIGTERM handling and
    machine-readable ``[RUN]`` summary without reimplementing it.  ``apply``
    and ``bump`` currently use it; a command supplies a reconcile hook only
    when its own action semantics require one.

    ``body`` receives the freshly created :class:`ApplyProgress` (with
    ``run_id`` already set) and returns ``failed`` the same way
    ``commands/apply.py::_run`` did. SIGTERM is registered for the duration
    of the call and restored (LIFO-safe, via the previous handler captured
    before installing ours) on the way out, so a nested/re-entrant call
    (e.g. under future orchestration) does not clobber an outer caller's
    handler.

    The durable ledger is intentionally non-reentrant: one live owner PID
    holds the SQLite-backed supervised-command lease. A concurrent or nested
    start is rejected without touching the active row; only a row whose owner
    PID is confirmed dead (or a legacy row without owner metadata) is recovered
    as ``orphaned``.

    SIGINT deliberately gets NO custom ``signal.signal`` handler here --
    only the default ``KeyboardInterrupt`` it already raises is caught
    below. Installing a custom SIGINT handler would change what type of
    exception propagates through Playwright/pipeline code and would change
    ``detail`` from ``"SIGINT"`` to ``"signal=2"``; #462 requires apply's
    behaviour and tests to stay identical, so SIGINT handling is
    intentionally left exactly as it already worked before this extraction.

    ``reconcile`` is an optional hook invoked inside the same protected
    ``finally`` block, right before ``finish_command_run`` and the
    ``[RUN]`` summary print, to let a caller reconcile ``progress`` against
    its own action-log semantics (apply's ``command_run_action_counts``
    query filters ``action='apply'`` -- that is apply-specific, not generic,
    so it is injected rather than hardcoded here; a future bump/publish
    caller would pass its own reconcile or none at all).
    """
    try:
        run_id = history.start_command_run(command=command, requested_limit=requested_limit)
    except CommandRunBusy as exc:
        print(f"[FAIL] {exc}")
        return True
    progress = ApplyProgress(run_id=run_id)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _terminate(signum, _frame):  # noqa: ANN001
        raise SignalTermination(signum)

    signal.signal(signal.SIGTERM, _terminate)
    final_status = "failed"
    exit_code = 1
    detail = None
    result: bool | CommandExitCode = True
    try:
        failed = body(progress)
        if isinstance(failed, CommandExitCode):
            # Typed terminal outcomes (currently expired authentication) must
            # reach the CLI unchanged instead of becoming generic exit 1.
            final_status = "failed"
            exit_code = failed.value
            result = failed
        else:
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
    except SignalTermination as exc:
        final_status = "interrupted"
        exit_code = 128 + exc.signum
        detail = f"signal={exc.signum}"
        result = CommandExitCode.SIGTERM
    except NotAuthenticated as exc:
        # A confirmed unauthenticated page is terminal before any action.  It
        # is intentionally not recorded as an uncertain action: that state is
        # reserved for the post-click grey zone where hh.ru may have accepted
        # the request.  Keep the reason in the command ledger and expose a
        # dedicated status for schedulers/automation.
        final_status = "failed"
        exit_code = CommandExitCode.SESSION_EXPIRED.value
        detail = f"{type(exc).__name__}: {exc}"
        print(
            f"[FAIL] Сессия hh.ru недействительна: {exc}. "
            "Выполните `hhru login` или `hhru refresh-token`, затем повторите."
        )
        result = CommandExitCode.SESSION_EXPIRED
    except BaseException as exc:
        detail = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        # cycle-review PR #460 (round 3, Claude /review), preserved verbatim
        # through this extraction: this bookkeeping can itself raise (e.g.
        # history.finish_command_run's ValueError when the run row is no
        # longer 'running') while a real exception from `body` is already
        # propagating through the `except BaseException: raise` above -- an
        # exception raised here would replace/mask it (standard Python
        # finally semantics), silently swallowing the original crash.
        # Log-and-continue instead: the ledger row staying 'running'/stale is
        # benign (`orphaned` already exists as the recognized terminal status
        # for exactly this kind of leftover, recovered on the next command
        # run's start_command_run()).
        try:
            if reconcile is not None:
                reconcile(progress, history, run_id)
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
            if print_summary:
                print(progress.summary(final_status))
        except Exception:
            logger.exception(
                "%s: не удалось финализировать durable ledger run_id=%s "
                "(поглощено, чтобы не заслонить исходное исключение)",
                command,
                run_id,
            )
    return result


def run_single_mutation_command(
    *,
    command: str,
    args: argparse.Namespace,
    body: Callable[[argparse.Namespace, ApplyProgress], bool],
) -> bool | CommandExitCode:
    """Thin ``run(args)`` for the single-mutation resume-edit commands (#465).

    Collapses the identical 8-line wrapper that ``edit_education.py``/
    ``edit_experience.py``/``edit_skills.py``/``edit_languages.py``/
    ``resume_position.py`` each duplicated verbatim (cycle-review PR #472,
    /code-review finding): open one ``History`` against ``args.history``,
    hand it to ``run_supervised_command`` with ``requested_limit=1`` (each of
    these commands performs at most one mutation per invocation), and forward
    ``progress`` into the command's own ``_run(args, progress)``.
    """
    history = History(getattr(args, "history", "data/history.db"))
    return run_supervised_command(
        command=command,
        history=history,
        requested_limit=1,
        body=lambda progress: body(args, progress),
    )

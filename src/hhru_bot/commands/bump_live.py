"""Команда bump-live: поднять резюме через живую вкладку (#1161, этап 2 #588).

CLI -> канал S1 (live-serve, #1159) -> исполнитель S2 в расширении hhru-live
(#1160) -> клик поднятия на списке /applicant/resumes, открытом пользователем
в Chrome. Браузер CLI не запускает; боевая семантика полностью переиспользована
(кулдаун 4ч can_bump_now, дневной лимит, --dry-run, те же action-имена и
статусы в history, throttle.wait после реального клика) — новым здесь является
только транспорт клика. Боевой bump продолжает работать как раньше: пути
сосуществуют.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..exit_codes import CommandExitCode
from ..history import History
from ..throttle import LimitReached, Throttle
from ._audit import action_status, record_resume_action
from ._common import ApplyProgress, add_common_args, resumes_from_args, run_supervised_command

# Как долго ждать подключения расширения к каналу (расширение reconnect'ится
# само; пользователь может открыть вкладку уже после старта команды).
CLIENT_TIMEOUT_SECONDS = 120.0


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "bump-live",
        help=(
            "Поднять резюме в поиске через живую вкладку "
            "(канал live-serve + расширение hhru-live; READ-local транспорт)"
        ),
    )
    add_common_args(p)
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="Порт канала на 127.0.0.1 (0 — свободный ephemeral, печатается при старте)",
    )
    p.set_defaults(func=run)


def _run(args: argparse.Namespace, config, history, progress: ApplyProgress) -> bool:
    # Импорт из модуля scenarios, не из пакета: до мержа #1159 пакет
    # hhru_bot.live — namespace (без __init__.py), имена живут в scenarios.
    from ..live.scenarios import ChannelError, LiveChannel, bump_via_live

    resumes = resumes_from_args(config, args)
    throttle = Throttle(config.throttle, history)
    failed = False

    channel = LiveChannel(port=args.port, client_timeout=CLIENT_TIMEOUT_SECONDS)
    url = channel.start()
    wait_s = f"{CLIENT_TIMEOUT_SECONDS:.0f}"
    print(f"[INFO] bump-live: канал {url} — жду расширение hhru-live (до {wait_s} с)")
    try:
        channel.wait_client()
    except ChannelError as exc:
        print(f"[FAIL] bump-live: {exc}")
        channel.close()
        return True

    try:
        for resume in resumes:
            print(f"\n=== Поднятие резюме: {resume.id} ===")

            try:
                throttle.check_bump_limit(resume.resume_id, args.dry_run)
            except LimitReached as e:
                print(f"Пропуск: {e}")
                continue

            can_bump, wait_left = throttle.can_bump_now(resume.resume_id)
            if not can_bump:
                print(f"Пропуск: рано поднимать, подождите ещё {wait_left}")
                continue

            progress.begin_attempt()
            result = bump_via_live(channel, resume, args.dry_run)

            # Семантика записи — ровно как у боевого bump (#163/#176): в actions
            # пишутся только реальные взаимодействия (acted), dry-run молчит.
            if result.acted:
                status = action_status(
                    dry_run=args.dry_run, success=result.success, uncertain=result.uncertain
                )
                record_resume_action(
                    history,
                    resume.resume_id,
                    "bump",
                    status,
                    result.reason,
                    run_id=progress.run_id,
                )

            if result.success:
                progress.applied_count += 1
                print(f"  [OK] {resume.id} поднято")
            else:
                progress.failed_count += 1
                failed = True
                print(f"  [FAIL] {resume.id} — {result.reason}")

            # Анти-бан-пауза только после реального действия — тот же инвариант,
            # что у боевого пути (CLAUDE.md: не убирать троттлинг).
            if result.acted:
                throttle.wait(f"после поднятия резюме '{resume.id}' (live-канал)")
    finally:
        channel.close()
    return failed


def run(args: argparse.Namespace) -> bool | CommandExitCode:
    """Run bump-live under its own durable command ledger entry."""
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    history = History(args.history)

    from .bump import _reconcile_bump_progress

    def _body(progress: ApplyProgress) -> bool:
        return _run(args, config, history, progress)

    return run_supervised_command(
        command="bump-live",
        history=history,
        requested_limit=None,
        body=_body,
        reconcile=_reconcile_bump_progress,
    )

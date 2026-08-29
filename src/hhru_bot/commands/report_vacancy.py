"""Команда report-vacancy: пожаловаться на вакансию на hh.ru (issue #745).

WRITE-hh-ru команда, ключуется по vacancy_id (не по резюме — жалоба не
привязана к конкретному резюме, см. ``clear_negotiations.ACCOUNT_SCOPE`` для
того же паттерна). Дневные лимиты/троттлинг ``throttle.py`` намеренно НЕ
применяются: это ручное, редкое, требующее осознанного выбора действие
(issue #745 явно запрещает автоматизацию/батч по эвристике), а не откликный
конвейер.

ВАЖНО (issue #745, зафиксированное ограничение): разведка селекторов
подтвердила только первые два шага трёхшагового wizard'а жалобы (причина →
комментарий). Финальный шаг подтверждения отправки НЕ исследован и не будет
кликаться — см. ``report_vacancy.report_vacancy_on_hh``. Эта команда поэтому
**никогда не переводит статус жалобы в success**: она доходит до заполненной
формы и печатает [FAIL], сообщая, что дальше нужно действовать вручную в
обычном браузере. Это осознанный дизайн, а не баг.
"""

from __future__ import annotations

import argparse

from ..selector_groups.vacancy_complain import VACANCY_COMPLAIN_REASON_IDS
from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command
from .copy_resume import confirm_write


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "report-vacancy",
        help="Дойти до формы жалобы на вакансию (причина+комментарий), НЕ отправлять",
        description=(
            "Открывает вакансию, кликает 'Ещё' -> 'Пожаловаться на вакансию', "
            "выбирает причину и заполняет комментарий. Останавливается ПЕРЕД "
            "финальной отправкой (issue #745): этот шаг не подтверждён живым DOM "
            "и намеренно не реализован. Всегда завершается [FAIL] — жалоба "
            "не отправляется этой командой ни при каких условиях."
        ),
    )
    p.add_argument(
        "--vacancy-id",
        required=True,
        help="ID вакансии (число из URL https://hh.ru/vacancy/<id>)",
    )
    p.add_argument(
        "--reason",
        required=True,
        choices=VACANCY_COMPLAIN_REASON_IDS,
        help="Причина жалобы (подтверждённый перечень hh.ru)",
    )
    p.add_argument(
        "--comment",
        required=True,
        help="Комментарий к жалобе (обязателен для всех причин на hh.ru)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Показать план без открытия формы (по умолчанию; --force доходит до формы)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Дойти до заполненной формы жалобы (без отправки — см. описание команды)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..report_vacancy import report_vacancy_on_hh
    from ..responses import NotAuthenticated

    if not args.comment.strip():
        print("[FAIL] Комментарий не может быть пустым — hh.ru требует его для любой причины.")
        raise SystemExit(1)

    config = load_config_or_exit(args.config)
    # Fail closed: --force is the sole switch that can leave dry-run.
    dry_run = not args.force
    history = History(args.history)

    if not dry_run and not confirm_write(
        args.force,
        prompt=(
            f"Дойти до формы жалобы на вакансию {args.vacancy_id} "
            f"(причина={args.reason})? Жалоба НЕ будет отправлена этой командой"
        ),
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не открыто."
        )
        raise SystemExit(1)

    # Дедупликация per-vacancy, не account-wide: begin_action пишет один и тот
    # же ID в оба поля actions (resume_id, vacancy_id) — переиспользуем
    # vacancy_id как "resume_id" параметр, иначе uncertain по одной вакансии
    # заблокировал бы report-vacancy для ВСЕХ остальных вакансий навсегда
    # (эта команда по дизайну никогда не пишет success, см. module docstring).
    if not dry_run and history.has_unresolved_uncertain(args.vacancy_id, "report_vacancy"):
        print(
            f"[FAIL] Предыдущая попытка report-vacancy для vacancy={args.vacancy_id} не "
            "подтверждена (uncertain). Проверьте состояние вакансии на hh.ru "
            "вручную перед повтором."
        )
        raise SystemExit(1)

    def _body(progress: ApplyProgress) -> bool:
        attempt = (
            None
            if dry_run
            else DurableMutationAttempt(history, progress, args.vacancy_id, "report_vacancy")
        )
        try:
            with launch_context(
                config.storage_state_file, headless=args.headless, user_agent=config.user_agent
            ) as context:
                result = report_vacancy_on_hh(
                    context.new_page(),
                    args.vacancy_id,
                    args.reason,
                    args.comment,
                    dry_run,
                    before_click=attempt.before_click if attempt is not None else None,
                )
        except NotAuthenticated as exc:
            print(f"[FAIL] Сессия недействительна: {exc}")
            return True
        except BaseException as exc:
            if attempt is not None:
                attempt.interrupt(exc)
            raise

        if attempt is not None:
            attempt.finish(result)

        if dry_run:
            print(f"[DRY-RUN] vacancy={args.vacancy_id} — {result.reason_text}")
            print("[INFO] Ничего не открыто.")
            return False

        # Эта команда по дизайну никогда не считает жалобу отправленной
        # (см. module docstring) — result.success всегда False.
        prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
        print(f"{prefix} vacancy={args.vacancy_id} — {result.reason_text}")
        return True

    return run_supervised_command(
        command=getattr(args, "command", "report-vacancy"),
        history=history,
        requested_limit=None,
        body=_body,
    )

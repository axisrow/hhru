"""Команда apply-live: отклик на вакансию через живую вкладку (#1162, этап 2 #588).

Клик по кнопке отклика, форма (оба shape), письмо и submit исполняются
расширением hhru-live в открытой пользователем вкладке Chrome (канал live-serve
#1159 + исполнитель #1160). CLI не мутирует hh.ru напрямую: мутирующие клики
идут через policy-ядро с явной авторизацией сценария (allowApply).

Боевая семантика переиспользована, не переписана: дедупликация has_applied,
дневной лимит, throttle.wait после реального действия, durable begin_action
(#245) и запись статусов — тот же контракт, что у боевого apply
(commands/apply_service._execute_apply_wave). Серая зона #207 финализируется
внешним источником истины — verify_response_in_negotiations — в ОТДЕЛЬНОМ
read-only Playwright-контексте с storage_state аккаунта (чтение SSR/DOM —
не скрытый запрос; мутирующих вызовов page.request нет, страж это проверяет).
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from ..exit_codes import CommandExitCode
from ..history import History
from ..throttle import LimitReached, Throttle
from ._common import ApplyProgress, add_common_args, resolve_resume, run_supervised_command

if TYPE_CHECKING:
    from ..config import AppConfig

# Сколько ждать подключения расширения к каналу (reconnect сам, вкладка может
# быть открыта позже старта команды).
CLIENT_TIMEOUT_SECONDS = 120.0
# Расширение подключается к ws://127.0.0.1:8765 жёстко (LIVE_SERVE_URL,
# background.js) — дефолт порта согласован с ним, а не ephemeral.
DEFAULT_LIVE_PORT = 8765


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "apply-live",
        help=(
            "Откликнуться на вакансию в живой вкладке "
            "(канал live-serve + расширение hhru-live; серая зона #207)"
        ),
    )
    add_common_args(p, max_pages_default=None)
    p.add_argument(
        "--vacancy",
        required=True,
        metavar="URL|ID",
        help="Вакансия-цель: URL страницы вакансии или числовой ID",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_LIVE_PORT,
        help=f"Порт канала на 127.0.0.1 (расширение слушает {DEFAULT_LIVE_PORT})",
    )
    p.set_defaults(func=run)


def _run(
    args: argparse.Namespace, config: AppConfig, history: History, progress: ApplyProgress
) -> bool:
    from ..apply.antibot import raise_for_antibot
    from ..apply.letter import render_cover_letter
    from ..apply.verify import verify_response_in_negotiations
    from ..blacklist import match as blacklist_match
    from ..browser import launch_context, require_authenticated_session
    from ..config import is_resume_url_placeholder
    from ..history import SKIP_REASONS
    from ..live.scenarios import ChannelError, LiveChannel, apply_via_live
    from ..search import (
        VacancyPageUnavailable,
        _extract_vacancy_id,
        current_employer_hit,
        fetch_vacancy_card,
    )
    from .apply_service import _prepare_apply_resume

    vacancy_id = _extract_vacancy_id(args.vacancy or "")
    if not vacancy_id:
        print(f"[FAIL] --vacancy ожидает числовой ID из URL вакансии: {args.vacancy}")
        return True
    if not args.resume:
        print("[FAIL] apply-live адресует одну вакансию — укажите явный --resume")
        return True
    resume = resolve_resume(config, args.resume)
    if is_resume_url_placeholder(resume.resume_url):
        print(
            f"[FAIL] {resume.id} — в конфиге указан плейсхолдер resume_url; "
            "укажите реальный URL (получить можно через list-resumes)"
        )
        return True

    throttle = Throttle(config.throttle, history)
    try:
        throttle.check_apply_limit(resume.resume_id, args.dry_run)
    except LimitReached as e:
        print(f"Пропуск: {e}")
        return False
    if history.has_applied(resume.resume_id, vacancy_id):
        print(f"[skip] отклик по резюме {resume.id} на {vacancy_id} уже есть в истории")
        return False

    # Identity + внешний источник серой зоны — read-only Playwright-контекст
    # со storage_state аккаунта (тот же приём, что у боевого apply; мутирующих
    # вызовов нет). Успех подтверждается только совпадением резюме (#212).
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        verify_page = context.new_page()
        require_authenticated_session(verify_page)
        identity = _prepare_apply_resume(verify_page, resume, args.dry_run)
        raise_for_antibot(verify_page)
        if identity is None:
            return True
        try:
            card = fetch_vacancy_card(verify_page, vacancy_id)
        except VacancyPageUnavailable as e:
            print(f"[FAIL] Вакансия недоступна: {e}")
            return True
        raise_for_antibot(verify_page)

        if (blacklist_reason := blacklist_match(card, history.blacklist_sets())) is not None:
            history.record_skip(resume.resume_id, vacancy_id, SKIP_REASONS.BLACKLIST)
            print(f"[skip] {blacklist_reason} — отклик не отправлен")
            return False
        if current_employer_hit(card.company, resume.search.current_employers) is not None:
            history.record_skip(resume.resume_id, vacancy_id, SKIP_REASONS.CURRENT_EMPLOYER)
            print("[skip] вакансия текущего работодателя — отклик не отправлен")
            return False

        def _verify(vac: str, _resume_id: str):
            # Хэш конфига подменяется числовым id для сравнения с SSR (#212);
            # запись в history и троттл остаются в домене хэша.
            return verify_response_in_negotiations(
                verify_page,
                vac,
                identity.verify_resume_id,
                account_resume_ids=identity.account_resume_ids,
                run_id=progress.run_id,
            )

        letter = render_cover_letter(config.cover_letter_for(resume), card)

        # Пикер резюме обязателен, если аккаунт мульти-резюме (или маппинг не
        # получен): hh.ru иначе приложит дефолтное резюме (11/11 боевых фактов
        # #1144). Единственное резюме аккаунта — hh.ru приложит его и так.
        require_select = identity.account_resume_ids is None or len(identity.account_resume_ids) > 1

        channel = LiveChannel(port=args.port, client_timeout=CLIENT_TIMEOUT_SECONDS)
        try:
            url = channel.start()
            wait_s = f"{CLIENT_TIMEOUT_SECONDS:.0f}"
            print(f"[INFO] apply-live: канал {url} — жду расширение hhru-live (до {wait_s} с)")
            channel.wait_client()
        except ChannelError as exc:
            print(f"[FAIL] apply-live: {exc}")
            channel.close()
            return True

        action_id = None

        def _before_submit() -> None:
            # #245: durable-маркер сразу перед кликом, который может отправить
            # отклик (в live это клик самой кнопки отклика — one-click shape).
            nonlocal action_id
            action_id = history.begin_action(
                resume.resume_id,
                vacancy_id,
                "apply",
                search_query=resume.search.text,
                run_id=progress.run_id,
            )

        progress.begin_attempt()
        try:
            result = apply_via_live(
                channel,
                resume,
                card,
                letter,
                args.dry_run,
                verify=None if args.dry_run else _verify,
                before_submit=_before_submit,
                require_resume_select=require_select,
            )
        finally:
            channel.close()

    if result.question_texts and not history.record_questionnaire_pending(
        resume.resume_id,
        [
            {
                "text": text,
                "kind": "text",
                "reason": "apply-live: census текста вопроса, канал не отвечает на анкеты сам",
            }
            for text in result.question_texts
        ],
        vacancy_id=vacancy_id,
        vacancy_url=card.url,
        run_id=progress.run_id,
    ):
        print("[FAIL] не удалось записать очередь неотвеченных вопросов анкеты")
        return True

    if result.skipped:
        progress.finish(result)
        # Зарезервированная строка не должна остаться 'uncertain' на skip-пути —
        # тот же маппинг, что у боевого _execute_apply_wave (ветка skipped).
        if action_id is not None:
            history.finalize_action(
                action_id, "failed", result.reason, reason_code=result.skip_reason or "skipped"
            )
        history.record_skip(
            resume.resume_id, vacancy_id, result.skip_reason or SKIP_REASONS.HAS_QUESTIONS
        )
        print(f"  [skip] {card.title} — {result.reason}")
        return False

    status = "uncertain" if result.uncertain else ("success" if result.success else "failed")
    if action_id is not None:
        history.finalize_action(action_id, status, result.reason, reason_code=status)
    elif result.acted:
        history.record_action(
            resume.resume_id,
            vacancy_id,
            "apply",
            status,
            result.reason,
            search_query=resume.search.text,
            run_id=progress.run_id,
            reason_code=status,
        )
    progress.finish(result)

    if result.success:
        print(f"  [OK] {card.title} — {card.company}" + (" (dry-run)" if args.dry_run else ""))
    else:
        print(f"  [FAIL] {card.title} — {result.reason}")

    # Анти-бан-пауза только после реального действия — тот же инвариант,
    # что у боевого пути (CLAUDE.md: не убирать троттлинг).
    if result.acted:
        throttle.wait(f"после отклика на '{card.title}' (live-канал)")
    return not result.success


def run(args: argparse.Namespace) -> bool | CommandExitCode:
    """Run apply-live under its own durable command ledger entry."""
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    history = History(args.history)

    def _body(progress: ApplyProgress) -> bool:
        return _run(args, config, history, progress)

    from .apply import _reconcile_from_action_log

    return run_supervised_command(
        command="apply-live",
        history=history,
        requested_limit=None,
        body=_body,
        reconcile=_reconcile_from_action_log,
    )

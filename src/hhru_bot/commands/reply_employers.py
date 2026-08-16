"""Reply to employers in chats selected from the local responses history."""

from __future__ import annotations

import argparse
import sys

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "reply-employers",
        help="Ответить работодателям в чатах",
        description=(
            "Account-wide ответы в чатах: план из локальной истории, финальная "
            "проверка живого чата и запись аудита."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать план без отправки")
    parser.add_argument("--limit", type=int, default=0, help="Максимум чатов за запуск (0 = все)")
    parser.add_argument(
        "--template", type=str, help="Текст ответа (по умолчанию cover_letter_default)"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить боевой запуск")
    parser.set_defaults(func=run)


def _letter(template: str, candidate: dict) -> str:
    from ..apply.letter import render_cover_letter
    from ..search import VacancyCard

    card = VacancyCard(
        vacancy_id=str(candidate["vacancy_id"]),
        title=str(candidate["title"]),
        company=str(candidate.get("employer") or ""),
        url=f"https://hh.ru/vacancy/{candidate['vacancy_id']}",
    )
    return render_cover_letter(template, card)


def run(args: argparse.Namespace) -> None:
    from ..browser import goto_hh, launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..negotiations_chat import (
        needs_reply,
        read_chat,
        send_reply_current,
        wait_reply_confirmation,
    )
    from ..negotiations_probe import topic_refs
    from ..throttle import LimitReached, Throttle

    if args.limit < 0:
        print("[FAIL] --limit не может быть отрицательным", file=sys.stderr)
        sys.exit(1)
    if not args.dry_run and not confirm_write(
        args.force,
        prompt="Ответить работодателям в выбранных чатах?",
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не отправлено."
        )
        sys.exit(1)

    config = load_config_or_exit(args.config)
    history = History(args.history)
    throttle = Throttle(config.throttle, history)
    candidates = history.reply_candidates(args.limit or None)
    template = args.template if args.template is not None else config.cover_letter_default
    print("=== Ответы работодателям (account-wide) ===")
    if not candidates:
        print("[INFO] В локальной истории нет чатов для проверки.")
        return

    sent = 0
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        goto_hh(page, "https://hh.ru/applicant/negotiations")
        topic_list = topic_refs(page.content())
        refs = {ref.topic_id: ref.chat_id for ref in topic_list}
        # #200: SSR отдаёт resumeId для каждой переписки (проверено на живой
        # сессии 2026-08-16, 7/7). Отдельный словарь, а не расширение refs:
        # read_chat принимает Mapping[str, str] topic→chat_id, и менять его
        # контракт ради аналитического поля незачем.
        resume_by_topic = {ref.topic_id: ref.resume_id for ref in topic_list}
        for candidate in candidates:
            topic = str(candidate["topic"])
            label = f"{candidate['vacancy_id']} «{candidate['title']}» @ {candidate['employer']}"
            chat = read_chat(page, topic, refs)
            decision = needs_reply(chat)
            if not decision.should_reply:
                print(f"[FAIL] {label} — {decision.reason}")
                continue
            assert chat is not None
            if history.has_replied(topic, chat.inbound_marker or ""):
                print(f"[skip] {label} — уже отвечали на это сообщение")
                continue
            letter = _letter(template, candidate)
            inbound_marker = chat.inbound_marker or ""
            status = "dry_run" if args.dry_run else "failed"
            reason = "dry-run" if args.dry_run else None
            if args.dry_run:
                print(f"[DRY-RUN] -> {label}\n    Письмо:\n    {letter}")
            else:
                try:
                    throttle.check_reply_limit(False)
                except LimitReached as exc:
                    print(f"[FAIL] {label} — {exc}")
                    break
                # Codex-ревью (#198): между планированием (needs_reply выше) и
                # отправкой прошло время (рендер письма, проверка лимита) —
                # чат перечитываем непосредственно перед кликом, чтобы
                # TOCTOU-окно не пропустило входящее от работодателя или наш
                # собственный ответ с другого устройства между этими шагами.
                live_chat = read_chat(page, topic, refs)
                live_decision = needs_reply(live_chat)
                if not live_decision.should_reply:
                    reason = f"чат изменился перед отправкой: {live_decision.reason}"
                    print(f"[FAIL] {label} — {reason}")
                    history.record_reply_and_action(
                        topic,
                        inbound_marker,
                        vacancy_id=str(candidate["vacancy_id"]),
                        resume_id=resume_by_topic.get(topic),
                        status="failed",
                        reason=reason,
                    )
                    continue
                assert live_chat is not None
                # Codex-ревью round 2 (#198): дедуплицируем и журналируем по
                # marker'у из ЖИВОГО перечтения, не из исходного планирования.
                # Если между первым read_chat и live-перечтением пришло НОВОЕ
                # входящее (а не наш собственный ответ — тот live_decision уже
                # отсёк выше), отвечаем фактически на него; журналирование
                # старого marker'а оставило бы новое входящее выглядящим
                # неотвеченным, и следующий запуск отправил бы дубликат.
                inbound_marker = live_chat.inbound_marker or ""
                try:
                    send_reply_current(page, letter)
                    # Клик мог не дойти (отклонение сервером, сетевой сбой) —
                    # success пишем только по позитивному подтверждению
                    # (последнее сообщение в чате стало нашим), как в
                    # apply/success.py (#7): таймаут даёт false-negative
                    # (status='failed'), не false-positive success.
                    #
                    # Codex-ревью round 2 (#198) отметил, что неподтверждённый
                    # клик мог реально дойти (DOM просто не успел отрендерить
                    # сигнал) — тогда 'failed' разрешает retry и риск
                    # дубликата. #176 в apply/bump решает это статусом
                    # 'uncertain'; для replies такое расширение НЕ вводим —
                    # REPLY_STATUS_VALUES сознательно заморожен решением #55
                    # («без машины состояний»), а issue #110 явно требует
                    # fail-closed в сторону «лучше пропустить чат, чем
                    # ответить повторно» — 'uncertain' с недедуплицирующей
                    # семантикой этому противоречил бы. Follow-up: #201.
                    if wait_reply_confirmation(page):
                        status = "success"
                        reason = None
                        sent += 1
                        print(f"[OK] {label}")
                    else:
                        reason = "отправка не подтверждена: нет сигнала доставки"
                        print(f"[FAIL] {label} — {reason}")
                except Exception as exc:
                    reason = f"отправка не подтверждена: {exc}"
                    print(f"[FAIL] {label} — {reason}")
                finally:
                    throttle.wait(f"после ответа в чате {topic}")
            history.record_reply_and_action(
                topic,
                inbound_marker,
                vacancy_id=str(candidate["vacancy_id"]),
                resume_id=resume_by_topic.get(topic),
                status=status,
                reason=reason,
            )

    print(f"Итого отправлено: {sent} ({'dry-run' if args.dry_run else 'боевой режим'})")

"""Reply to employers in chats selected from the local responses history."""

from __future__ import annotations

import argparse
import sys

from ._common import ApplyProgress, run_supervised_command
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
        "--max-pages",
        type=int,
        default=5,
        help="Максимум страниц negotiations для SSR mapping (по умолчанию 5)",
    )
    parser.add_argument(
        "--template", type=str, help="Текст ответа (по умолчанию cover_letter_default)"
    )
    parser.add_argument(
        "--suggest",
        action="store_true",
        help="Сгенерировать и сохранить draft по входящему сообщению (без отправки)",
    )
    parser.add_argument(
        "--follow-up",
        action="store_true",
        help=(
            "Режим напоминания (#710): вместо ответа на входящее — напомнить о себе "
            "там, где последнее слово уже за нами и работодатель молчит --after-days N"
        ),
    )
    parser.add_argument(
        "--after-days",
        type=int,
        help="Порог молчания работодателя в днях для --follow-up (обязателен вместе с ним)",
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


def _run(args: argparse.Namespace, config, history, progress: ApplyProgress) -> bool:
    from ..browser import launch_context
    from ..negotiations_chat import (
        NoReplyForm,
        count_visible_messages,
        is_robot_questionnaire,
        needs_follow_up,
        needs_reply,
        read_chat,
        send_reply_current,
        wait_reply_confirmation,
    )
    from ..negotiations_probe import paginated_topic_and_remindable_refs, paginated_topic_refs
    from ..responses import NotAuthenticated, ResponsesIndeterminate
    from ..throttle import LimitReached, Throttle

    follow_up = getattr(args, "follow_up", False)
    max_pages = getattr(args, "max_pages", 5)
    throttle = Throttle(config.throttle, history)
    if follow_up:
        candidates = history.follow_up_candidates(args.after_days, args.limit or None)
        template = args.template if args.template is not None else config.follow_up_letter
        heading = (
            f"=== Напоминания работодателям (account-wide, --after-days {args.after_days}) ==="
        )
        decide = needs_follow_up
    else:
        candidates = history.reply_candidates(args.limit or None)
        template = args.template if args.template is not None else config.cover_letter_default
        heading = "=== Ответы работодателям (account-wide) ==="
        decide = needs_reply
    print(heading)
    if not template:
        template_flag = "follow_up_letter" if follow_up else "cover_letter_default"
        print(f"[FAIL] Пустой шаблон письма (--template или config.{template_flag}).")
        return True
    if not candidates:
        print("[INFO] В локальной истории нет чатов для проверки.")
        return False

    sent = 0
    failed = False
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        # #201: пагинируем SSR chat mapping по всем страницам negotiations
        # (аналогично --max-pages в других командах), иначе чат, ушедший за
        # пределы первой страницы, тихо выглядит как empty_chat. Та же пара
        # исключений, что и у fetch_responses (её собственный вызывающий код
        # в responses.py ловит их так же): истёкшая сессия или не
        # подтверждённая пагинация не должны крашить команду с traceback.
        remindable_topics: set[str] | None = None
        try:
            if follow_up:
                # #710: локальная история говорит «работодатель молчит N дней»,
                # но финальное разрешение на напоминание — прерогатива hh.ru
                # (responseReminderState.allowed), а не эвристика по возрасту.
                # Один обход даёт ОБА SSR-представления одного и того же HTML
                # (topic→chat mapping + remindable-флаги) — раздельные вызовы
                # paginated_topic_refs()+paginated_remindable_topic_refs()
                # удвоили бы реальные браузерные переходы по тем же страницам
                # negotiations без всякой новой информации.
                topic_list, remindable_refs = paginated_topic_and_remindable_refs(
                    page, max_pages=max_pages
                )
                remindable_topics = {ref.topic_id for ref in remindable_refs}
            else:
                # #201: пагинируем SSR chat mapping по всем страницам
                # negotiations (аналогично --max-pages в других командах),
                # иначе чат, ушедший за пределы первой страницы, тихо
                # выглядит как empty_chat.
                topic_list = paginated_topic_refs(page, max_pages=max_pages)
        except (NotAuthenticated, ResponsesIndeterminate, ValueError) as exc:
            # ValueError (#710, cycle-review round 2): remindable_topic_refs()
            # -- в отличие от topic_refs(), который молча дропает битые
            # записи -- намеренно строгий и бросает ValueError на дрейфе
            # SSR-схемы (см. negotiations_probe.py). Без этого класса в except
            # дрейф разметки hh.ru печатал бы голый traceback вместо [FAIL]
            # (тот же дефект, что #747/#748 чинил для goto_hh). Тот же except
            # покрывает обе ветки: ResponsesIndeterminate/NotAuthenticated
            # остаются достижимы и для обычного пути через topic_refs().
            print(f"[FAIL] не удалось прочитать SSR chat mapping: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        refs = {ref.topic_id: ref.chat_id for ref in topic_list}
        refs_by_topic = {}
        for ref in topic_list:
            refs_by_topic.setdefault(ref.topic_id, []).append(ref)
        # #200: SSR отдаёт resumeId для каждой переписки (проверено на живой
        # сессии 2026-08-16, 7/7). Отдельный словарь, а не расширение refs:
        # read_chat принимает Mapping[str, str] topic→chat_id, и менять его
        # контракт ради аналитического поля незачем.
        resume_by_topic = {ref.topic_id: ref.resume_id for ref in topic_list}
        for candidate in candidates:
            topic = str(candidate["topic"])
            label = f"{candidate['vacancy_id']} «{candidate['title']}» @ {candidate['employer']}"
            live_resume_id = resume_by_topic.get(topic)
            if remindable_topics is not None and topic not in remindable_topics:
                print(f"[skip] {label} — hh.ru не разрешает напоминание для этого чата")
                progress.skipped_count += 1
                continue
            if getattr(args, "suggest", False):
                live_refs = [
                    ref
                    for ref in refs_by_topic.get(topic, [])
                    if ref.vacancy_id == str(candidate["vacancy_id"]) and ref.resume_id is not None
                ]
                if len(live_refs) != 1:
                    print(f"[skip] {label} — ambiguous live vacancy/resume mapping")
                    progress.skipped_count += 1
                    continue
                live_resume_id = live_refs[0].resume_id
            chat = read_chat(page, topic, refs)
            if chat is not None and is_robot_questionnaire(chat.conversation or (chat,)):
                history.mark_robot_questionnaire(
                    topic, vacancy_id=str(candidate["vacancy_id"]), reason="robot_questionnaire"
                )
                print(f"[skip] {label} — robot-questionnaire (ручная очередь)")
                progress.skipped_count += 1
                continue
            if history.is_robot_questionnaire(topic):
                print(f"[skip] {label} — robot-questionnaire (ручная очередь)")
                progress.skipped_count += 1
                continue
            decision = decide(chat)
            if not decision.should_reply:
                # /code-review high: "last_message_from_us" is the routine
                # state of an already-answered chat waiting on the employer,
                # not an error -- it fires on nearly every normal account-wide
                # sweep once some chats are answered. Before durable-run
                # wiring, run() always returned None and this branch never
                # affected the exit code at all (cli.py's fail-closed
                # contract, #148, was never opt-in for reply-employers).
                # Keeping it a routine skip preserves that behaviour; only
                # genuine DOM-read uncertainty (empty_chat/inbound_marker_
                # unknown/author_unknown) is a real failure worth failing the
                # run and a nonzero exit code for. --follow-up mirrors this:
                # "last_message_from_employer" (they already answered, or we
                # already sent the reminder and they replied) is the routine
                # skip there instead.
                routine_reason = (
                    "last_message_from_employer" if follow_up else "last_message_from_us"
                )
                if decision.reason == routine_reason:
                    print(f"[skip] {label} — {decision.reason}")
                    progress.skipped_count += 1
                else:
                    print(f"[FAIL] {label} — {decision.reason}")
                    progress.failed_count += 1
                    failed = True
                continue
            assert chat is not None
            # #710: для follow-up нет нового входящего сообщения, дедуплицируем
            # по marker'у самого затишья (status_changed_at кандидата), а не по
            # chat.inbound_marker (это marker НАШЕГО последнего сообщения и не
            # меняется, пока работодатель молчит — has_replied() иначе увидел
            # бы точно тот же marker после каждой отправки и заблокировал бы
            # даже первое напоминание). Переиспользует has_replied как просил
            # issue #710 — тот же дедуп-барьер, свой namespace marker'а.
            dedup_marker = (
                f"follow_up:{candidate['status_changed_at']}"
                if follow_up
                else (chat.inbound_marker or "")
            )
            if history.has_replied(topic, dedup_marker):
                skip_reason = (
                    "уже напоминали об этом молчании"
                    if follow_up
                    else "уже отвечали на это сообщение"
                )
                print(f"[skip] {label} — {skip_reason}")
                progress.skipped_count += 1
                continue
            # --follow-up/--suggest несовместимость проверена в run() до входа
            # сюда (sys.exit(1)); эта ветка её не дублирует, чтобы не оставлять
            # недостижимый код на случай прямого вызова _run() в обход run().
            if getattr(args, "suggest", False):
                from ..ai.llm_client import LLMClient
                from ..reply_suggestions import ReplyContext, suggest

                inbound = chat.conversation[-1] if chat.conversation else chat
                try:
                    letter = suggest(
                        [
                            ReplyContext(
                                topic=topic,
                                inbound_marker=chat.inbound_marker or "",
                                inbound_text=inbound.text,
                                vacancy_id=str(candidate["vacancy_id"]),
                                vacancy_title=str(candidate["title"]),
                                employer=str(candidate.get("employer") or ""),
                                resume_id=resume_by_topic.get(topic),
                            )
                        ],
                        LLMClient(config.ai),
                    )
                    history.save_reply_draft(
                        topic=topic,
                        inbound_marker=chat.inbound_marker or "",
                        vacancy_id=str(candidate["vacancy_id"]),
                        resume_id=live_resume_id,
                        message=letter,
                    )
                    print(f"[DRAFT] {label}\n    Ответ:\n    {letter}")
                    progress.skipped_count += 1
                    continue
                except Exception as exc:
                    print(f"[FAIL] {label} — suggestion не создан: {exc}")
                    progress.failed_count += 1
                    failed = True
                    continue
            letter = _letter(template, candidate)
            progress.begin_attempt()
            inbound_marker = dedup_marker
            status = "dry_run" if args.dry_run else "failed"
            reason = "dry-run" if args.dry_run else None
            action_id = None
            if args.dry_run:
                print(f"[DRY-RUN] -> {label}\n    Письмо:\n    {letter}")
            else:
                try:
                    throttle.check_reply_limit(False)
                except LimitReached as exc:
                    print(f"[FAIL] {label} — {exc}")
                    progress.failed_count += 1
                    failed = True
                    break
                # Codex-ревью (#198): между планированием (needs_reply выше) и
                # отправкой прошло время (рендер письма, проверка лимита) —
                # чат перечитываем непосредственно перед кликом, чтобы
                # TOCTOU-окно не пропустило входящее от работодателя или наш
                # собственный ответ с другого устройства между этими шагами.
                # #710 (cycle-review round 2): remindable_topics — снимок ДО
                # цикла кандидатов, не перепроверяется здесь. hh.ru теоретически
                # может отозвать разрешение на напоминание в этом узком окне
                # (throttle-задержка предыдущего кандидата + рендер письма),
                # но повторная проверка стоила бы ещё одной навигации на
                # /applicant/negotiations на каждого кандидата — тот же
                # анти-фрод trade-off, из-за которого reply/remindable читаются
                # ОДНИМ обходом, а не двумя (см. paginated_topic_and_remindable_
                # refs). Окно уже сужено содержательной TOCTOU-проверкой чата
                # ниже (decide(live_chat)): реальный новый ответ работодателя
                # по-прежнему блокирует отправку.
                live_chat = read_chat(page, topic, refs)
                live_decision = decide(live_chat)
                if not live_decision.should_reply:
                    reason = f"чат изменился перед отправкой: {live_decision.reason}"
                    print(f"[FAIL] {label} — {reason}")
                    history.record_reply_and_action(
                        topic,
                        inbound_marker,
                        vacancy_id=str(candidate["vacancy_id"]),
                        resume_id=live_resume_id,
                        status="failed",
                        reason=reason,
                        run_id=progress.run_id,
                    )
                    progress.failed_count += 1
                    failed = True
                    continue
                assert live_chat is not None
                # Codex-ревью round 2 (#198): дедуплицируем и журналируем по
                # marker'у из ЖИВОГО перечтения, не из исходного планирования.
                # Если между первым read_chat и live-перечтением пришло НОВОЕ
                # входящее (а не наш собственный ответ — тот live_decision уже
                # отсёк выше), отвечаем фактически на него; журналирование
                # старого marker'а оставило бы новое входящее выглядящим
                # неотвеченным, и следующий запуск отправил бы дубликат.
                # #710: для follow-up живой inbound_marker — это marker НАШЕГО
                # собственного последнего сообщения (он не меняется, пока
                # работодатель молчит), поэтому дедуп-идентичность follow-up
                # остаётся синтетическим marker'ом затишья, а не живым чтением.
                inbound_marker = dedup_marker if follow_up else (live_chat.inbound_marker or "")
                # Codex adversarial review (cycle-review PR #471, round 1): the
                # durable action row must exist BEFORE the send click, mirroring
                # apply's before_submit / clear-negotiations' begin_action
                # pre-click barrier. Without it, a SIGINT/SIGTERM landing between
                # this click and the post-confirmation write below leaves no
                # actions row at all -- reconcile() then folds the attempt into
                # an ordinary 'failed' count instead of the fail-closed
                # 'uncertain' that a possibly-delivered message requires (#176
                # applies here exactly as it does to apply/withdraw).
                action_id = history.begin_action(
                    resume_by_topic.get(topic) or "",
                    str(candidate["vacancy_id"]),
                    "reply",
                    search_query=None,
                    run_id=progress.run_id,
                )
                # #710 (cycle-review round 2): for a plain reply, the message
                # BEFORE the click is the employer's -- "last message is ours"
                # after the click is itself new evidence. For --follow-up the
                # precondition (needs_follow_up) is the mirror: the last
                # message is ALREADY ours before the click, so that same
                # signal is true regardless of whether the click delivered
                # anything. min_count anchors wait_reply_confirmation() to a
                # strictly higher message count, the only signal a follow-up
                # actually rendered a new message.
                pre_click_count = count_visible_messages(page) if follow_up else 0
                try:
                    send_reply_current(page, letter)
                except NoReplyForm as exc:
                    # Форма не найдена ДО какого-либо взаимодействия с DOM —
                    # чистый pre-action early-exit, на hh.ru следа нет. Как и
                    # другие ранние выходы до действия (#163), throttle.wait()
                    # здесь не нужен: не от чего защищать паузой.
                    reason = f"отправка не выполнена: {exc}"
                    status = "failed"
                    print(f"[FAIL] {label} — {reason}")
                except Exception as exc:
                    # Исключение уже ПОСЛЕ начала клика (Codex #201, по
                    # аналогии с #176 в apply/bump): fill()/click() могли
                    # частично выполниться и сообщение — уйти на hh.ru,
                    # несмотря на исключение. fail-closed в сторону «действие
                    # могло случиться»: status='uncertain' (не дедуплицирует
                    # has_replied), а не 'failed' (который бы разрешил тихий
                    # повторный retry поверх реально ушедшего сообщения).
                    reason = f"клик выполнен, исход неопределён: {exc}"
                    status = "uncertain"
                    print(f"[FAIL] {label} — {reason}")
                    throttle.wait(f"после ответа в чате {topic}")
                else:
                    # Клик мог не дойти (отклонение сервером, сетевой сбой) —
                    # success пишем только по позитивному подтверждению
                    # (последнее сообщение в чате стало нашим), как в
                    # apply/success.py (#7): таймаут не даёт false-positive
                    # success, но после состоявшегося клика фиксируется как
                    # uncertain, а не как безопасный для retry failed.
                    min_count = pre_click_count + 1 if follow_up else 1
                    if wait_reply_confirmation(page, min_count=min_count):
                        status = "success"
                        reason = None
                        sent += 1
                        print(f"[OK] {label}")
                    else:
                        reason = "отправка не подтверждена: нет сигнала доставки"
                        # The click completed, but the positive DOM signal may
                        # have rendered late. Keep this auditable without
                        # making has_replied deduplicate it.
                        status = "uncertain"
                        print(f"[FAIL] {label} — {reason}")
                    # Клик состоялся (успешно или uncertain) — реальное
                    # действие на hh.ru, пауза нужна (#163).
                    throttle.wait(f"после ответа в чате {topic}")
            if action_id is not None:
                # Pre-click reservation exists (begin_action above). Codex
                # adversarial review (cycle-review PR #471, round 3): finalizing
                # the action and journaling the reply as two separate
                # transactions left a crash window where a confirmed external
                # reply's action row was finalized but its replies row never
                # committed -- has_replied() (dedup barrier #12) reads only
                # replies, so that crash silently reopened the duplicate-send
                # guard. finalize_reply_action() commits both in one
                # transaction, matching record_reply_and_action's atomicity for
                # the non-reserved (dry-run/pre-click-failed) path below.
                history.finalize_reply_action(
                    action_id,
                    topic,
                    inbound_marker,
                    vacancy_id=str(candidate["vacancy_id"]),
                    resume_id=resume_by_topic.get(topic),
                    status=status,
                    reason=reason,
                )
            else:
                history.record_reply_and_action(
                    topic,
                    inbound_marker,
                    vacancy_id=str(candidate["vacancy_id"]),
                    resume_id=resume_by_topic.get(topic),
                    status=status,
                    reason=reason,
                    run_id=progress.run_id,
                )
            if status == "success":
                progress.applied_count += 1
            elif status == "uncertain":
                progress.uncertain_count += 1
                failed = True
            elif status == "dry_run":
                progress.skipped_count += 1
            else:
                progress.failed_count += 1
                failed = True

    print(f"Итого отправлено: {sent} ({'dry-run' if args.dry_run else 'боевой режим'})")
    return failed


def _reconcile(progress: ApplyProgress, history, run_id: str) -> None:
    counts = history.command_run_action_counts(run_id, action="reply")
    progress.applied_count = max(progress.applied_count, counts.get("success", 0))
    progress.failed_count = max(progress.failed_count, counts.get("failed", 0))
    progress.uncertain_count = max(progress.uncertain_count, counts.get("uncertain", 0))
    completed = (
        progress.applied_count
        + progress.failed_count
        + progress.uncertain_count
        + progress.skipped_count
    )
    if progress.attempted_count > completed:
        progress.failed_count += progress.attempted_count - completed


def run(args: argparse.Namespace):
    """Reply under a durable command run without changing reply deduplication."""
    from ..config import load_config_or_exit
    from ..history import History

    if args.limit < 0:
        print("[FAIL] --limit не может быть отрицательным", file=sys.stderr)
        sys.exit(1)
    max_pages = getattr(args, "max_pages", 5)
    if max_pages < 1:
        print(f"[FAIL] --max-pages должен быть >= 1 (получено {max_pages}).", file=sys.stderr)
        sys.exit(1)
    follow_up = getattr(args, "follow_up", False)
    after_days = getattr(args, "after_days", None)
    if follow_up and after_days is None:
        print("[FAIL] --follow-up требует --after-days N", file=sys.stderr)
        sys.exit(1)
    if not follow_up and after_days is not None:
        print("[FAIL] --after-days действует только вместе с --follow-up", file=sys.stderr)
        sys.exit(1)
    if after_days is not None and after_days < 1:
        print(f"[FAIL] --after-days должен быть >= 1 (получено {after_days}).", file=sys.stderr)
        sys.exit(1)
    if follow_up and getattr(args, "suggest", False):
        print("[FAIL] --suggest несовместим с --follow-up", file=sys.stderr)
        sys.exit(1)
    prompt = (
        "Напомнить о себе работодателям в выбранных чатах?"
        if follow_up
        else "Ответить работодателям в выбранных чатах?"
    )
    if (
        not args.dry_run
        and not getattr(args, "suggest", False)
        and not confirm_write(args.force, prompt=prompt)
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не отправлено."
        )
        sys.exit(1)
    config = load_config_or_exit(args.config)
    history = History(args.history)
    return run_supervised_command(
        command=getattr(args, "command", "reply-employers"),
        history=history,
        requested_limit=args.limit or None,
        body=lambda progress: _run(args, config, history, progress),
        reconcile=_reconcile,
    )

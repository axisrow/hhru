"""Ответ роботу-анкете кнопкой быстрых ответов (Да/Нет) в чате hh.ru.

Очередь робот-анкет ведёт reply-employers (skip → robot_questionnaires);
до этой команды очередь была тупиковой — append-only без резолва. robot-reply
нажимает кнопку под вопросом бота и резолвит строку очереди колонкой
resolved_at (не DELETE: факт обнаружения хранит аудит).

WRITE-hh-ru: боевой клик требует --force или TTY-подтверждения; dry-run
проходит весь путь ДО begin_action (goto → чтение чата → поиск кнопок →
план) и не кликает. Uncertain-механика #176: pre-click begin_action, клик →
подтверждение wait_reply_confirmation (позитивный сигнал — новое сообщение
нашего авторства), таймаут/исключение после клика = uncertain, резолв
очереди только по подтверждённому success.
"""

from __future__ import annotations

import argparse
import sys

from ._common import ApplyProgress, run_supervised_command
from .copy_resume import confirm_write

_DEFAULT_HYDRATION_WAIT_MS = 6000


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "robot-reply",
        help="Ответить роботу-анкете кнопкой быстрых ответов (Да/Нет)",
        description=(
            "Нажимает кнопку быстрых ответов под вопросом робота в чате "
            "(очередь: robot-queue). WRITE-hh-ru: боевой клик требует --force "
            "или подтверждения; dry-run показывает план без клика."
        ),
    )
    parser.add_argument("--topic", required=True, help="ID topic из robot-queue")
    parser.add_argument(
        "--answer",
        required=True,
        help="Точный текст кнопки (например «Да» или «Нет»)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать план без клика")
    parser.add_argument("--force", action="store_true", help="Подтвердить боевой клик")
    parser.add_argument(
        "--any-topic",
        action="store_true",
        help=(
            "Разрешить topic вне очереди robot-queue (обход гейта очереди; "
            "--force остаётся чистой авторизацией клика)"
        ),
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=_DEFAULT_HYDRATION_WAIT_MS,
        help="Бюджет ожидания гидратации кнопок в мс (по умолчанию 6000)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Максимум страниц negotiations для SSR topic→chat mapping",
    )
    parser.set_defaults(func=run)


def _run(args: argparse.Namespace, config, history, progress: ApplyProgress) -> bool:
    from ..browser import launch_context
    from ..negotiations_chat import (
        NoQuickReply,
        click_quick_reply,
        count_visible_messages,
        find_quick_replies,
        needs_reply,
        read_chat,
        wait_reply_confirmation,
    )
    from ..negotiations_probe import paginated_topic_refs
    from ..responses import NotAuthenticated, ResponsesIndeterminate
    from ..throttle import LimitReached, Throttle

    throttle = Throttle(config.throttle, history)
    topic = args.topic.strip()
    row = history.robot_questionnaire_row(topic)
    if row is None and not args.any_topic:
        print(f"[FAIL] topic {topic} не в очереди robot-queue (см. hhru robot-queue)")
        return True
    already_resolved = bool(row is not None and row.get("resolved_at"))
    resolved_answer = str((row or {}).get("answer") or "?")
    resolved_at = str((row or {}).get("resolved_at") or "")
    vacancy_id = str((row or {}).get("vacancy_id") or "")

    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        try:
            topic_list = paginated_topic_refs(page, max_pages=args.max_pages)
        except (NotAuthenticated, ResponsesIndeterminate, ValueError) as exc:
            print(f"[FAIL] не удалось прочитать SSR chat mapping: {exc}", file=sys.stderr)
            return True
        refs = {ref.topic_id: ref.chat_id for ref in topic_list}
        if topic not in refs:
            print(
                f"[FAIL] topic {topic} не найден в живом negotiations-списке "
                "(переписка удалена или ушла за --max-pages)"
            )
            return True
        chat = read_chat(page, topic, refs)
        decision = needs_reply(chat)
        if not decision.should_reply:
            if already_resolved:
                # Дедуп повторного клика: резолв стоит И нового вопроса нет —
                # последнее сообщение чата наше (робот молчит). Новый вопрос
                # робота делает should_reply истинным и повтор разрешён.
                print(
                    f"[FAIL] topic {topic} уже отвечен: {resolved_answer} "
                    f"({resolved_at}) — нового вопроса робота нет"
                )
            else:
                print(
                    f"[FAIL] кнопка не нажимается: последнее сообщение чата не требует "
                    f"ответа ({decision.reason})"
                )
            return True
        if already_resolved:
            print(
                f"[INFO] Робот задал новый вопрос после ответа "
                f"«{resolved_answer}» — отвечаем повторно"
            )
        assert chat is not None
        question = chat.text
        buttons = find_quick_replies(page, timeout_ms=args.wait_ms)
        if args.answer not in buttons:
            available = ", ".join(buttons) if buttons else "нет кнопок (уже отвечено?)"
            print(f"[FAIL] кнопки «{args.answer}» нет. Доступные кнопки: {available}")
            return True
        print(f"[INFO] Вопрос робота: {question}")
        print(f"[INFO] Кнопки: {', '.join(buttons)}")
        if args.dry_run:
            print(f"[DRY-RUN] Нажал бы кнопку «{args.answer}» — клика не было")
            progress.skipped_count += 1
            return False

        try:
            throttle.check_reply_limit(False)
        except LimitReached as exc:
            print(f"[FAIL] {exc}")
            return True
        progress.begin_attempt()
        inbound_marker = chat.inbound_marker or ""
        pre_click_count = count_visible_messages(page)
        # Pre-click durable-барьер (#176): строка actions ДО клика, чтобы
        # SIGINT/SIGTERM между кликом и подтверждением не терял попытку.
        action_id = history.begin_action("", vacancy_id, "robot_reply", run_id=progress.run_id)
        status: str
        reason: str | None
        try:
            click_quick_reply(page, args.answer)
        except NoQuickReply as exc:
            # Кнопка не резолвится однозначно ДО клика — чистый pre-action
            # early-exit (#163): на hh.ru следа нет, throttle.wait не нужен.
            status = "failed"
            reason = str(exc)
            print(f"[FAIL] {topic} — {reason}")
        except Exception as exc:  # noqa: BLE001 - классифицируем по клик-границе
            # Исключение уже ПОСЛЕ начала клика — fail-closed uncertain (#176).
            status = "uncertain"
            reason = f"клик выполнен, исход неопределён: {exc}"
            print(f"[FAIL] {topic} — {reason}")
            throttle.wait(f"после ответа роботу в чате {topic}")
        else:
            if wait_reply_confirmation(page, min_count=pre_click_count + 1):
                status = "success"
                reason = None
                print(f"[OK] {topic} — кнопка «{args.answer}» нажата, доставка подтверждена")
            else:
                status = "uncertain"
                reason = "отправка не подтверждена: нет сигнала доставки"
                print(f"[FAIL] {topic} — {reason}")
            throttle.wait(f"после ответа роботу в чате {topic}")
        history.finalize_reply_action(
            action_id,
            topic,
            inbound_marker,
            vacancy_id=vacancy_id,
            status=status,
            reason=reason,
        )
        if status == "success":
            progress.applied_count += 1
            history.resolve_robot_questionnaire(topic, answer=args.answer)
        elif status == "uncertain":
            progress.uncertain_count += 1
        else:
            progress.failed_count += 1
        return status != "success"


def _reconcile(progress: ApplyProgress, history, run_id: str) -> None:
    """Reconcile по action='robot_reply' (своё action-имя: аудит не смешивается
    с шаблонными письмами reply-employers)."""
    counts = history.command_run_action_counts(run_id, action="robot_reply")
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
    """Один клик по кнопке робота под durable command-run ledger."""
    from ..config import load_config_or_exit
    from ..history import History

    if not args.topic.strip() or not args.answer.strip():
        print("[FAIL] --topic и --answer обязательны и непусты", file=sys.stderr)
        return True
    if args.wait_ms < 0:
        print("[FAIL] --wait-ms не может быть отрицательным", file=sys.stderr)
        return True
    if not args.dry_run and not confirm_write(
        args.force, prompt=f"Нажать кнопку «{args.answer}» в чате робота ({args.topic})?"
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Кнопка не нажата."
        )
        return True
    config = load_config_or_exit(args.config)
    history = History(args.history)
    return run_supervised_command(
        command=getattr(args, "command", "robot-reply"),
        history=history,
        requested_limit=None,
        body=lambda progress: _run(args, config, history, progress),
        reconcile=_reconcile,
    )

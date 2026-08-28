"""Команда responses: мониторинг ответов работодателей (#12, Этап 2).

Top-level команда ``hhru_bot responses ...`` — регистрируется автоматически через
pkgutil.iter_modules в cli.register_commands (cli.py не трогается).

Поток: открыть /applicant/negotiations → responses.fetch_responses собирает
карточки → history.upsert_response по каждому (account-scope, без клонирования
по резюме) → печать ASCII-сводки «что нового» (status_changed_at с последней
отметки).

Read-only по hh.ru: страница откликов только читается, кликов действий нет.
Вывод только текст/ASCII — НИКАКИХ эмодзи (правило проекта: CLI чистый).
"""

from __future__ import annotations

import argparse
import sys

from ..exit_codes import CommandExitCode


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("должно быть положительным целым числом")
    return parsed


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "responses",
        help="Проверить ответы работодателей (приглашения/отказы/сообщения)",
    )
    p.add_argument("--resume", help="ID резюме из конфига (по умолчанию — все)")
    p.add_argument(
        "--max-pages",
        type=_positive_int,
        default=5,
        help="Максимум страниц списка откликов (по умолчанию 5)",
    )
    p.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="Показать ответы, сменившие статус за последние N часов (по умолчанию 24). "
        "0 — выполнить живой обход hh.ru и показать синхронизацию/историю.",
    )
    p.add_argument(
        "--detect-external-tests",
        action="store_true",
        help="Прочитать последние сообщения работодателей и записать внешние тесты (#180)",
    )
    p.add_argument(
        "--remindable",
        action="store_true",
        help="Показать переписки, для которых hh.ru явно разрешает напоминание",
    )
    p.add_argument(
        "--sync-applied",
        action="store_true",
        help="Импортировать однозначные ручные/внешние отклики в dedup ledger",
    )
    p.add_argument(
        "--alert-new",
        action="store_true",
        help="Сообщить о новых приглашениях и вернуть специальный exit-код",
    )
    p.add_argument(
        "--calendar-hint",
        action="store_true",
        help="Для каждого нового приглашения напечатать готовую команду "
        "`hhru calendar event` с плейсхолдерами времени (#711)",
    )
    p.set_defaults(func=run)


def _response_employer_label(row: dict) -> str:
    """Человекочитаемый работодатель из строки responses, тот же, что в таблице."""
    return (row.get("employer") or "").strip() or "(скрыт)"


def _calendar_hint_line(employer: str, vacancy_id: str) -> str:
    """Готовая к копированию строка вызова ``calendar event`` для приглашения.

    Время — ВСЕГДА плейсхолдер (#711): hh.ru не сообщает время собеседования в
    статусе приглашения, а вывести его из ``response_date``/``status_changed_at``
    значило бы догадаться — догадка в календаре означает пропущенное интервью
    (см. контракт в ``commands/calendar.py:3-5``). Пользователь подставляет
    реальное время сам.

    ``summary`` экранируется через ``shlex.quote`` — работодатель может
    содержать кавычки (напр. `ООО "Ромашка"`), которые иначе сломали бы
    copy-paste команды в shell.
    """
    import shlex

    summary = f"{employer} - {vacancy_id or '(скрыт)'}"
    return (
        f"hhru calendar event --summary {shlex.quote(summary)} "
        "--start <YYYY-MM-DDTHH:MM:SS+TZ> --end <YYYY-MM-DDTHH:MM:SS+TZ>"
    )


def _print_calendar_hints(rows: list[dict]) -> None:
    """Печатает calendar-hint для каждого приглашения из ``rows`` (#711).

    ``rows`` — уже отфильтрованная выборка «новых» ответов (см.
    ``history.new_responses_since``); функция сама отбирает только
    ``status == 'invitation'``, не трогая остальные статусы.
    """
    invitations = [r for r in rows if r.get("status") == "invitation"]
    if not invitations:
        return
    print(f"\n[INFO] Calendar hint для новых приглашений: {len(invitations)}")
    for row in invitations:
        print(_calendar_hint_line(_response_employer_label(row), row.get("vacancy_id", "")))


def _print_responses_table(rows: list[dict], title: str) -> None:
    """ASCII-таблица ответов. rows — dict'и из history.new_responses_since."""
    print(f"\n{title}: {len(rows)}")
    if not rows:
        print("  (нет новых ответов за период)")
        return

    # Колонки фиксированной ширины для читаемого выравнивания (чистый ASCII).
    headers = ("Вакансия", "Работодатель", "Статус", "Дата", "Изменён")
    # Статус-ключ → человекочитаемая метка для вывода (storage хранит ключ).
    status_label = {
        "invitation": "Приглашение",
        "response": "Ответ",
        "discard": "Отказ",
        "read": "Прочитано",
        "unknown": "?",
    }
    body = []
    for r in rows:
        vac = r.get("vacancy_id", "")
        emp = _response_employer_label(r)
        st = status_label.get(r.get("status", ""), r.get("status", "") or "?")
        # Дата ответа с hh.ru как есть (текстовый блок карточки); «-» если hh.ru
        # не отдал блок даты.
        date = (r.get("response_date") or "").strip() or "-"
        # Обрезаем ISO-время до минут: «2026-07-27 14:05» (полная секунда избыточна).
        changed = (r.get("status_changed_at") or "")[:16].replace("T", " ")
        body.append((vac, emp, st, date, changed))

    cols = list(zip(headers, *body, strict=False))
    widths = [max(len(str(c)) for c in col) for col in cols]

    def border() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells) -> str:
        return "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    print(border())
    print(line(headers))
    print(border())
    for row in body:
        print(line(row))
    print(border())


def run(args: argparse.Namespace) -> CommandExitCode | None:
    from datetime import datetime, timedelta

    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..responses import (
        NotAuthenticated,
        ResponsesIndeterminate,
        ResponseStatus,
        fetch_responses,
    )

    config = load_config_or_exit(args.config)
    history = History(args.history)

    # «Что нового» меряется по status_changed_at. since = now - since-hours.
    # --sync-applied всегда выполняет живой read, включая --since-hours 0.
    remindable_only = getattr(args, "remindable", False)
    sync_applied = getattr(args, "sync_applied", False)
    alert_new = getattr(args, "alert_new", False)
    detect_external_tests = getattr(args, "detect_external_tests", False)
    calendar_hint = getattr(args, "calendar_hint", False)
    if sync_applied and (remindable_only or detect_external_tests):
        print(
            "Ошибка: --sync-applied нельзя совмещать с --remindable или --detect-external-tests",
            file=sys.stderr,
        )
        sys.exit(2)
    if alert_new and (sync_applied or remindable_only or detect_external_tests):
        print(
            "Ошибка: --alert-new нельзя совмещать с --sync-applied, --remindable "
            "или --detect-external-tests",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.max_pages < 1:
        print("Ошибка: --max-pages должен быть положительным", file=sys.stderr)
        sys.exit(2)
    fresh_only = (
        args.since_hours <= 0 and not remindable_only and not sync_applied and not alert_new
    )
    scan_started_at = datetime.now()
    since_fetch = scan_started_at - timedelta(hours=args.since_hours)
    # Для сводки «что нового»: в режиме history-only берём вообще всё (min), иначе —
    # окно since-fetch. datetime.min — «любая status_changed_at подходит».
    since_summary = datetime.min if fresh_only else since_fetch
    alert_since = None
    if alert_new:
        try:
            # A first alert poll establishes a baseline at its start. Thus an
            # invitation already present in an old local history is not
            # reported, while one discovered by this poll is.
            alert_since = history.responses_alert_checkpoint() or scan_started_at
        except ValueError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            sys.exit(1)

    if fresh_only:
        print("\n=== Ответы работодателей (вся история, без обхода hh.ru) ===")
    elif not alert_new:
        print(f"\n=== Ответы работодателей (новое за {args.since_hours:g}ч) ===")

    # Responses — account-scope: страница /applicant/negotiations общая и НЕ несёт
    # достоверного признака принадлежности ответа конкретному резюме. Поэтому
    # карточки persist'ятся ОДИН РАЗ (одна строка на vacancy_id), БЕЗ клонирования
    # под все resume_id из конфига — клонирование фабриковало бы данные (ответ
    # резюме A приписывался бы и резюме B). --resume здесь warn+ignore: фильтр по
    # резюме для ответов работодателя невозможен без достоверной атрибуции.
    if args.resume is not None and not alert_new:
        print(
            "Внимание: --resume игнорируется как фильтр обхода — /applicant/negotiations "
            "сканируется на уровне аккаунта; подтверждённая SSR-атрибуция выводится "
            "отдельно как vacancy/topic/resume."
        )

    inserted = updated = unchanged = 0

    if not fresh_only:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            try:
                if remindable_only:
                    from ..negotiations_probe import paginated_remindable_topic_refs

                    refs = paginated_remindable_topic_refs(page, max_pages=args.max_pages)
                    print(f"Переписки с разрешённым напоминанием: {len(refs)}")
                    for ref in refs:
                        print(
                            f"topic={ref.topic_id} chat={ref.chat_id} "
                            f"работодатель={ref.employer or '-'} "
                            f"вакансия={ref.vacancy} vacancy_id={ref.vacancy_id}"
                        )
                    return
                cards = fetch_responses(
                    page,
                    max_pages=args.max_pages,
                    strict_empty=sync_applied,
                    strict_scrape=alert_new,
                )
                if alert_new and any(
                    card.topic_ambiguous and card.status == ResponseStatus.INVITATION
                    for card in cards
                ):
                    print(
                        "Ошибка: новое приглашение пропущено из-за неоднозначного topic; "
                        "checkpoint не обновлён",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if alert_new and alert_since == scan_started_at:
                    # The first poll has no durable baseline yet. Store it before
                    # incremental upserts so a partial write remains retryable.
                    history.mark_responses_alert_success(scan_started_at)
            except (NotAuthenticated, ResponsesIndeterminate, ValueError) as e:
                # Истёкшая сессия или не подтверждённый DOM: НЕ затираем
                # историю и НЕ выдаём неопределённость за «нет новых ответов».
                print(f"Ошибка: {e}", file=sys.stderr)
                sys.exit(1)
                return
            if sync_applied:
                try:
                    synced = history.sync_external_applied(cards)
                except ValueError as e:
                    print(f"Ошибка: {e}", file=sys.stderr)
                    sys.exit(1)
                    return
                print(
                    "Синхронизация внешних откликов: "
                    f"добавлено {synced['imported']}, "
                    f"ambiguous {synced['ambiguous']}, "
                    f"пропущено {synced['skipped']}"
                )
                return

            if detect_external_tests:
                # fetch_responses performs its own navigation. Re-open the
                # list read-only so SSR topicList is captured from the actual
                # negotiations page, then use the confirmed chatId route.
                from ..negotiations_chat import (
                    extract_external_test_link,
                    read_employer_messages,
                )
                from ..negotiations_probe import paginated_topic_refs

                refs = {
                    ref.topic_id: ref.chat_id
                    for ref in paginated_topic_refs(page, max_pages=args.max_pages)
                }
                detected = 0
                for card in cards:
                    if not card.topic or card.topic not in refs:
                        continue
                    for message_text in read_employer_messages(page, refs[card.topic]):
                        test_url = extract_external_test_link(message_text)
                        if test_url is None:
                            continue
                        # resume_id=None: как и responses (см. warn выше),
                        # args.resume здесь ничем не подтверждён.
                        # NB (#200): формулировка «привязки не существует»
                        # опровергнута — SSR topicList[] отдаёт resumeId, и
                        # topic_refs() его читает (TopicRef.resume_id). Здесь
                        # он пока не проброшен: это зона #180 (внешние тесты),
                        # менять её в рамках #200 не стали.
                        history.record_test_assigned(
                            card.resume_id,
                            card.vacancy_id,
                            card.topic,
                            card.employer,
                            test_url,
                            message_text,
                        )
                        detected += 1
                print(f"Назначений внешнего теста обнаружено: {detected}")

        if not alert_new:
            print(f"Собрано карточек переписки: {len(cards)}")

        skipped_ambiguous = 0
        for card in cards:
            if not alert_new:
                print(
                    "[CORRELATION] "
                    f"vacancy_id={card.vacancy_id} topic={card.topic or '-'} "
                    f"resume_id={card.resume_id or '-'}"
                )
            if card.topic_ambiguous:
                # Несколько SSR-topic кандидатов на одну вакансию — fetch_responses
                # намеренно оставил topic=None (см. ResponseItem.topic_ambiguous).
                # history.upsert_response матчит существующую строку по
                # (vacancy_id, topic IS NULL): персистить такую карточку наравне с
                # легитимными без-чата ответами слило бы разные переписки одной
                # вакансии в одну строку истории. Пропускаем запись, не гадаем.
                skipped_ambiguous += 1
                continue
            outcome = history.upsert_response(
                vacancy_id=card.vacancy_id,
                employer=card.employer or None,
                status=card.status,
                chat_url=card.chat_url,
                topic=card.topic,
                response_date=card.date or None,
                resume_id=card.resume_id,
            )
            if outcome == "inserted":
                inserted += 1
            elif outcome == "updated":
                updated += 1
            else:
                unchanged += 1

        if alert_new:
            # Bound the successful poll before reading history. Writes racing
            # this read then have a later watermark and remain for the next poll.
            alert_watermark = datetime.now()
            rows = history.new_responses_since(alert_since, resume_id=None)
            invitations = [
                row
                for row in rows
                if row.get("status") == "invitation"
                or (
                    row.get("last_invitation_at")
                    and row["last_invitation_at"] > alert_since.isoformat()
                )
            ]
            if invitations:
                print(f"[INFO] Новых приглашений: {len(invitations)}")
                for row in invitations:
                    employer = (row.get("employer") or "").strip() or "(скрыт)"
                    print(f"Вакансия: {row.get('vacancy_id', '')} | Работодатель: {employer}")
                sys.stdout.flush()
                history.mark_responses_alert_success(alert_watermark)
                return CommandExitCode.NEW_INVITATIONS
            history.mark_responses_alert_success(alert_watermark)
            return None

        print(
            f"Новых ответов: {inserted + updated} "
            f"(новых записей: {inserted}, смен статуса: {updated})"
        )
        if skipped_ambiguous:
            print(
                f"[WARN] Пропущено записей с неоднозначным topic: {skipped_ambiguous} "
                "(несколько переписок на одну вакансию, сопоставление с чатом не "
                "подтверждено — см. лог warning)"
            )
    else:
        print("Режим --since-hours 0: обход hh.ru пропущен, вывожу всю историю ответов.")

    # Сводка «что нового» по истории (account-scope — без фильтра по resume_id).
    rows = history.new_responses_since(since_summary)
    _print_responses_table(rows, "Новые ответы работодателей")
    if calendar_hint:
        _print_calendar_hints(rows)

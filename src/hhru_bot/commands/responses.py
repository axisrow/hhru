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


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "responses",
        help="Проверить ответы работодателей (приглашения/отказы/сообщения)",
    )
    p.add_argument("--resume", help="ID резюме из конфига (по умолчанию — все)")
    p.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Максимум страниц списка откликов (по умолчанию 5)",
    )
    p.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="Показать ответы, сменившие статус за последние N часов (по умолчанию 24). "
        "0 — показать все известные ответы из истории без нового обхода hh.ru.",
    )
    p.set_defaults(func=run)


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
        emp = (r.get("employer") or "").strip() or "(скрыт)"
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


def run(args: argparse.Namespace) -> None:
    from datetime import datetime, timedelta

    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..responses import NotAuthenticated, ResponsesIndeterminate, fetch_responses

    config = load_config_or_exit(args.config)
    history = History(args.history)

    # «Что нового» меряется по status_changed_at. since = now - since-hours.
    # since-hours<=0 → пользователь явно просит НЕ ходить на hh.ru, а показать
    # всё из истории (быстрый read-only дашборд без обхода).
    fresh_only = args.since_hours <= 0
    since_fetch = datetime.now() - timedelta(hours=args.since_hours)
    # Для сводки «что нового»: в режиме history-only берём вообще всё (min), иначе —
    # окно since-fetch. datetime.min — «любая status_changed_at подходит».
    since_summary = datetime.min if fresh_only else since_fetch

    if fresh_only:
        print("\n=== Ответы работодателей (вся история, без обхода hh.ru) ===")
    else:
        print(f"\n=== Ответы работодателей (новое за {args.since_hours:g}ч) ===")

    # Responses — account-scope: страница /applicant/negotiations общая и НЕ несёт
    # достоверного признака принадлежности ответа конкретному резюме. Поэтому
    # карточки persist'ятся ОДИН РАЗ (одна строка на vacancy_id), БЕЗ клонирования
    # под все resume_id из конфига — клонирование фабриковало бы данные (ответ
    # резюме A приписывался бы и резюме B). --resume здесь warn+ignore: фильтр по
    # резюме для ответов работодателя невозможен без достоверной атрибуции.
    if args.resume is not None:
        print(
            "Внимание: --resume игнорируется — ответы работодателя аккаунт-уровневые "
            "(страница /applicant/negotiations общая, принадлежность резюме недоступна)."
        )

    inserted = updated = unchanged = 0

    if not fresh_only:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            try:
                cards = fetch_responses(page, max_pages=args.max_pages)
            except (NotAuthenticated, ResponsesIndeterminate) as e:
                # Истёкшая сессия или не подтверждённый DOM: НЕ затираем
                # историю и НЕ выдаём неопределённость за «нет новых ответов».
                print(f"Ошибка: {e}", file=sys.stderr)
                sys.exit(1)
                return

        print(f"Собрано карточек переписки: {len(cards)}")

        for card in cards:
            outcome = history.upsert_response(
                vacancy_id=card.vacancy_id,
                employer=card.employer or None,
                status=card.status,
                chat_url=card.chat_url,
                topic=card.topic,
                response_date=card.date or None,
            )
            if outcome == "inserted":
                inserted += 1
            elif outcome == "updated":
                updated += 1
            else:
                unchanged += 1

        print(
            f"Новых ответов: {inserted + updated} "
            f"(новых записей: {inserted}, смен статуса: {updated})"
        )
    else:
        print("Режим --since-hours 0: обход hh.ru пропущен, вывожу всю историю ответов.")

    # Сводка «что нового» по истории (account-scope — без фильтра по resume_id).
    rows = history.new_responses_since(since_summary)
    _print_responses_table(rows, "Новые ответы работодателей")

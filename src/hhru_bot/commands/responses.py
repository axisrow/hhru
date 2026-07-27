"""Команда responses: мониторинг ответов работодателей (#12, Этап 2).

Top-level команда ``hhru_bot responses ...`` — регистрируется автоматически через
pkgutil.iter_modules в cli.register_commands (cli.py не трогается).

Поток: открыть /applicant/negotiations → responses.fetch_responses собирает
карточки → history.upsert_response по каждому (resume.resume_id, vacancy_id) →
печать ASCII-сводки «что нового» (status_changed_at с последней отметки).

Read-only по hh.ru: страница откликов только читается. Случайная пауза между
страницами списка (throttle.wait) — анти-фрод принцип CLAUDE.md сохранён.
Вывод только текст/ASCII — НИКАКИХ эмодзи (правило проекта: CLI чистый).
"""

from __future__ import annotations

import argparse

from ._common import resumes_from_args


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
    headers = ("Вакансия", "Работодатель", "Статус", "Изменён")
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
        # Обрезаем ISO-время до минут: «2026-07-27 14:05» (полная секунда избыточна).
        changed = (r.get("status_changed_at") or "")[:16].replace("T", " ")
        body.append((vac, emp, st, changed))

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
    from ..responses import fetch_responses
    from ..throttle import Throttle

    config = load_config_or_exit(args.config)
    history = History(args.history)
    resumes = resumes_from_args(config, args)
    throttle = Throttle(config.throttle, history)

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

    # Срез для итоговой сводки. upsert обновляет только при смене статуса;
    # vacancy_id, по которому собрали ответ, не привязан к resume в самой карточке
    # hh.ru (страница общая по аккаунту) — поэтому ответы апдейтим под КАЖДОЕ
    # резюме из конфига (как делает bump/apply, обходя resumes). Это даёт
    # корректный new_responses_since(resume_id) в дальнейшем. Если резюме одно —
    # записей ровно по нему; если несколько — по каждому (дубли по vacancy_id
    # разнесены resume_id, UNIQUE (resume_id, vacancy_id) не нарушается).
    inserted = updated = unchanged = 0

    if not fresh_only:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            cards = fetch_responses(page, max_pages=args.max_pages)

        print(f"Собрано карточек переписки: {len(cards)}")

        for resume in resumes:
            for card in cards:
                outcome = history.upsert_response(
                    resume_id=resume.resume_id,
                    vacancy_id=card.vacancy_id,
                    employer=card.employer or None,
                    status=card.status,
                    chat_url=card.chat_url,
                )
                if outcome == "inserted":
                    inserted += 1
                elif outcome == "updated":
                    updated += 1
                else:
                    unchanged += 1
            # Анти-бан: между обработкой резюме — случайная пауза (как apply/bump).
            # При одном резюме паузы нет (как и в apply для одного resume).
            if len(resumes) > 1:
                throttle.wait("после обхода ответов одного резюме")

        print(
            f"Новых ответов: {inserted + updated} "
            f"(новых записей: {inserted}, смен статуса: {updated})"
        )
    else:
        print("Режим --since-hours 0: обход hh.ru пропущен, вывожу всю историю ответов.")

    # Сводка «что нового» по истории (если резюме одно — фильтруем по нему,
    # иначе по всем — как stats). fresh_only → показываем все строки истории.
    resume_id = resumes[0].resume_id if len(resumes) == 1 else None
    rows = history.new_responses_since(since_summary, resume_id=resume_id)
    _print_responses_table(rows, "Новые ответы работодателей")

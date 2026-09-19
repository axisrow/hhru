"""census: показать, что РЕАЛЬНО отрисовано на странице (#1002).

Агенты «слепы»: сырой HTML-дамп содержит JSON-состояние и i18n-словари,
текстовые литералы которых неотличимы от UI при поиске по подстроке
(ложные «8 Городов» #998). Команда открывает URL и печатает census —
таблицу отрисованных DOM-контролов (data-qa/tag/role/label/text/visible).
Read-only: никаких кликов и отправок формы.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..browser import (
    census_table,
    goto_hh,
    has_login_form,
    launch_context,
    rendered_controls_census,
)
from ..exit_codes import CommandExitCode


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "census",
        help="Таблица отрисованных контролов страницы (агентские «глаза», read-only)",
    )
    parser.add_argument("--url", required=True, help="Полный URL страницы hh.ru")
    parser.add_argument("--json", action="store_true", help="Машиночитаемый JSON-вывод")
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=0,
        help="Подождать N мс после загрузки перед снимком (гидратация React, #858)",
    )
    parser.add_argument(
        "--fill-text",
        help=(
            "ЛОКАЛЬНАЯ диагностика композера чата: ввести текст в поле ответа "
            "и снять census ПОСЛЕ него (кнопка отправки рендерится только при "
            "непустом вводе). Ничего не отправляется: кликов нет, страница "
            "закрывается вместе с черновиком"
        ),
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> bool | CommandExitCode:
    from ..config import load_config

    config = load_config(args.config)
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        goto_hh(page, args.url)
        if getattr(args, "wait_ms", 0):
            page.wait_for_timeout(args.wait_ms)
        if getattr(args, "fill_text", None):
            from ..selector_groups.negotiations import CHAT_MESSAGE_INPUT

            input_loc = page.locator(CHAT_MESSAGE_INPUT)
            if input_loc.count() != 1:
                print(
                    f"[FAIL] --fill-text: поле ответа не найдено однозначно ({CHAT_MESSAGE_INPUT})"
                )
                return True
            input_loc.fill(args.fill_text)
            # Рендер кнопки отправки реактивен: даём composers-фреймворку
            # отрисовать её после ввода (паттерн «commit не значит отрисовано»).
            page.wait_for_timeout(1500)
        rows = rendered_controls_census(page)
        # #1129: read-only census — «глаза» агента, и он НЕ должен принять
        # страницу входа за целевую (живой кейс: census my_resumes при
        # истёкшей сессии молча отрисовал форму login). Явная пометка и в
        # текстовом, и в JSON-выводе; сам вывод остаётся данными («ничего
        # не фейлит»), но вызывальщику отказ различается по exit-коду:
        # подтверждённая неаутентифицированная страница — SESSION_EXPIRED
        # (78), тот же код, что у supervised-команд (#1141).
        login_form_detected = has_login_form(page)

    visible_only = [r for r in rows if r.get("visible")]
    if getattr(args, "json", False):
        import json

        print("MACHINE_READABLE_JSON:")
        print(
            json.dumps(
                {"url": args.url, "login_form": login_form_detected, "controls": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
        return CommandExitCode.SESSION_EXPIRED if login_form_detected else False

    print(f"URL: {args.url}")
    if login_form_detected:
        print(
            "[INFO] ВНИМАНИЕ: на странице обнаружена форма входа hh.ru — "
            "сессия, вероятно, недействительна, census ниже описывает СТРАНИЦУ "
            "ВХОДА, а не целевую. Выполните login."
        )
    print(f"Контролов всего: {len(rows)}, видимых: {len(visible_only)}")
    print(census_table(visible_only))
    print("[OK] census read-only; вхождения строк в HTML-дампе (JSON/i18n) — не поля")
    return CommandExitCode.SESSION_EXPIRED if login_form_detected else False

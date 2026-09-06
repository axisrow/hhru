"""CLI command for the simple fields of the resume ``common`` screen (#876)."""

from __future__ import annotations

import argparse

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "common",
        help="Заполнить простые поля экрана common резюме",
        description=(
            "Заполняет через UI поля common, включая условия работы. "
            "area, metro и citizenship пока не входят в команду."
        ),
    )
    parser.add_argument("--resume", required=True, help="Slug из конфига или resume_id HH.ru")
    parser.add_argument("--first-name", dest="first_name", help="Имя")
    parser.add_argument("--last-name", dest="last_name", help="Фамилия")
    parser.add_argument("--birthday", help="Дата в формате, который принимает форма hh.ru")
    parser.add_argument("--gender", choices=("male", "female"), help="Пол")
    parser.add_argument("--phone", help="Телефон")
    parser.add_argument("--area", help="Точный leaf города из live-каталога hh.ru")
    parser.add_argument("--metro", help="Точная станция метро")
    parser.add_argument(
        "--citizenship",
        action="append",
        help="Точное гражданство из live-каталога; можно повторять",
    )
    parser.add_argument("--work-ticket", choices=("true", "false"), help="Трудовая книжка")
    parser.add_argument(
        "--relocation", choices=("ready", "consider", "not_ready"), help="Готовность к переезду"
    )
    parser.add_argument(
        "--schedule",
        action="append",
        choices=("full_day", "shift", "flexible", "remote"),
        help="График работы; можно указать несколько раз",
    )
    parser.add_argument(
        "--employment",
        action="append",
        choices=("full_time", "part_time", "internship", "volunteer"),
        help="Тип занятости; можно указать несколько раз",
    )
    parser.add_argument(
        "--work-format",
        action="append",
        choices=("office", "hybrid", "remote"),
        help="Формат работы; можно указать несколько раз",
    )
    parser.add_argument(
        "--business-trip",
        "--business-trips",
        dest="business_trip",
        choices=("true", "false"),
        help="Готовность к командировкам",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Показать фактические значения полей common (включая предзаполненные hh.ru) и выйти",
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать план без сохранения")
    parser.add_argument("--force", action="store_true", help="Подтвердить запись без prompt")
    parser.set_defaults(func=run)


def _print_current(current) -> None:
    """Таблица фактического состояния common: непусто / пусто.

    Происхождение непустого значения (предзаполнил hh.ru при открытии или
    сохранил ранее сам владелец) по одному снимку формы неразличимо, поэтому
    колонка утверждает только факт заполненности, а не источник.
    """
    from ..common import REQUIRED_FIELDS
    from ..report import _ascii_table

    rows = []
    for field, label in (
        ("first_name", "Имя"),
        ("last_name", "Фамилия"),
        ("birthday", "Дата рождения"),
        ("gender", "Пол"),
        ("phone", "Телефон"),
        ("area", "Город"),
        ("citizenship", "Гражданство"),
        # #997: контрол work-ticket-selector на визарде — «Разрешение на
        # работу» (display-only, записи нет); путать с трудовой книжкой нельзя.
        ("work_permit", "Разрешение на работу"),
        ("work_ticket", "Трудовая книжка"),
        ("relocation", "Готовность к переезду"),
        ("schedule", "График работы"),
        ("employment", "Тип занятости"),
        ("work_format", "Формат работы"),
        ("business_trip", "Командировки"),
    ):
        value = getattr(current, field)
        if value is None:
            rendered = ""
        elif isinstance(value, list):
            rendered = ", ".join(value)
        else:
            rendered = str(value)
        if not rendered.strip():
            state = "пусто"
        elif field in REQUIRED_FIELDS:
            state = "заполнено (обязательное)"
        else:
            state = "заполнено"
        rows.append([label, rendered, state])
    print(_ascii_table(["Поле", "Значение", "Состояние"], rows))


def _run(args: argparse.Namespace, progress) -> bool:
    from ..browser import BrowserLaunchError, NotAuthenticated, dump_page_html, launch_context
    from ..common import (
        CANCEL,
        CommonValues,
        merge_prefilled,
        missing_required,
        open_common_form,
        read_common,
        save_common,
    )
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ._common import DurableMutationAttempt, resolve_resume

    config = load_config_or_exit(args.config)
    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True
    values = CommonValues(
        first_name=args.first_name,
        last_name=args.last_name,
        birthday=args.birthday,
        gender=args.gender,
        phone=args.phone,
        area=getattr(args, "area", None),
        metro=(
            [args.metro]
            if getattr(args, "metro", None) is not None and isinstance(args.metro, str)
            else getattr(args, "metro", None)
        ),
        citizenship=getattr(args, "citizenship", None),
        work_ticket=getattr(args, "work_ticket", None),
        relocation=getattr(args, "relocation", None),
        schedule=getattr(args, "schedule", None),
        employment=getattr(args, "employment", None),
        work_format=getattr(args, "work_format", None),
        business_trip=getattr(args, "business_trip", None),
    )
    if not values.provided() and not args.show:
        print(
            "[INFO] Явных полей нет: работает авто-режим #982 — прочитаю "
            "предзаполненное hh.ru и сохраню его, если обязательные поля непусты."
        )
    history = History(args.history)
    # Ревью PR #986: uncertain-маркер edit_common (пишется и этим seam'ом, и
    # create-resume --fill-common) обязан блокировать повтор мутации для того
    # же resume_id до появления более поздней success-строки — тот же гейт,
    # что у publish/copy (#176/#476). Разрешение — только ручная
    # reconciliation через подтверждение на hh.ru.
    if not args.dry_run and history.has_unresolved_uncertain(resume.resume_id, "edit_common"):
        print(
            "[FAIL] предыдущее изменение common для этого резюме не подтверждено "
            "(uncertain). Проверьте состояние на hh.ru вручную перед повтором."
        )
        return True
    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            open_common_form(page, resume)
            try:
                current = read_common(page)
            except Exception:
                dump = dump_page_html(page, "common_failure")
                if dump:
                    print(f"[INFO] Дамп экрана common для разбора: {dump}")
                raise
            _print_current(current)
            if args.show:
                print("[OK] Значения common показаны; read-only, изменений на hh.ru нет")
                return False
            requested_any = bool(values.provided())
            values, skipped = merge_prefilled(values, current)
            for field, prefilled in skipped:
                print(f"[INFO] {field}: уже заполнено на hh.ru ({prefilled!r}) — не трогаю")
            if not values.provided():
                if requested_any:
                    print(
                        "[INFO] Все указанные поля уже заполнены на hh.ru — "
                        "заполнять нечего, сохранение не требуется."
                    )
                    return False
                missing = missing_required(current)
                if missing:
                    print(f"[FAIL] Обязательные поля common пусты: {', '.join(missing)}")
                    print("[FAIL] Ничего не сохранено; заполните их и повторите запуск")
                    return True
                print(
                    "[INFO] Все обязательные поля предзаполнены hh.ru — "
                    "сохраняю предзаполненное без изменений (#982)"
                )
            print(
                "[WARN] common — это общие данные профиля аккаунта; сохранение затронет "
                "все резюме аккаунта, включая опубликованные боевые резюме."
            )
            if values.provided():
                print("[INFO] План заполнения (только пустые поля):")
                for key, value in values.provided().items():
                    print(f"  {key}: {value}")
            if not args.dry_run and not confirm_write(
                args.force, prompt=f"Сохранить простые поля common резюме '{resume.id}' на hh.ru?"
            ):
                print(
                    "[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено."
                )
                return True
            if args.dry_run:
                page.locator(CANCEL).first.click()
                print("[DRY-RUN] save не нажат; изменений на hh.ru нет")
                return False
            attempt = DurableMutationAttempt(history, progress, resume.resume_id, "edit_common")
            result = save_common(page, values, before_click=attempt.before_click)
            attempt.finish(result)
    except BrowserLaunchError:
        raise
    except NotAuthenticated:
        # Preserve the supervisor's dedicated SESSION_EXPIRED classification
        # and its actionable login/refresh-token guidance.
        raise
    except Exception as exc:
        if progress.attempted_count:
            progress.finish(exc)
        print(f"[FAIL] {resume.id} — {exc}")
        return True
    if result.success:
        print(f"[OK] Простые поля common резюме '{resume.id}' сохранены.")
        return False
    prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
    print(f"{prefix} {resume.id} — {result.reason}")
    return True


def run(args: argparse.Namespace):
    from ._common import run_single_mutation_command

    return run_single_mutation_command(command="edit_common", args=args, body=_run)

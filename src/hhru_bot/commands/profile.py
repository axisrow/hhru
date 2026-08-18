"""Локальное управление единым профилем аккаунта.

``profile`` не открывает браузер и не меняет данные на hh.ru: значения
хранятся только в ``account_profile`` локальной SQLite-истории. Автоматически
считанные поля ``hh_ru`` не удаляются командой ``unset`` — она снимает только
ручное значение, после чего автоматически считанное снова видно в ответах.
"""

from __future__ import annotations

import argparse

from ..external_forms.detect import normalize


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "profile",
        help="Управление локальным профилем аккаунта",
        description="Установить, показать или удалить ручные ответы профиля.",
    )
    commands = parser.add_subparsers(dest="profile_command", required=True)

    set_parser = commands.add_parser("set", help="Установить ручное значение поля")
    set_parser.add_argument("label", help="Текст вопроса или подпись поля")
    set_parser.add_argument("value", help="Значение ответа")
    set_parser.set_defaults(func=run_set)

    show_parser = commands.add_parser("show", help="Показать все поля профиля")
    show_parser.set_defaults(func=run_show)

    unset_parser = commands.add_parser("unset", help="Удалить ручное значение поля")
    unset_parser.add_argument("label", help="Текст вопроса или подпись поля")
    unset_parser.set_defaults(func=run_unset)


def run_set(args: argparse.Namespace) -> None:
    from ..history import History

    History(args.history).upsert_profile_field(normalize(args.label), args.value, source="manual")
    print(f'[OK] Профиль обновлён: "{args.label}" = "{args.value}".')


def run_show(args: argparse.Namespace) -> None:
    from ..history import History
    from ..report import _ascii_table

    fields = History(args.history).list_profile_fields()
    rows = [
        [field["question_key"], field["value"], field["source"], field["updated_at"]]
        for field in fields
    ]
    print(_ascii_table(["question_key", "value", "source", "updated_at"], rows))


def run_unset(args: argparse.Namespace) -> None:
    from ..history import History

    label = args.label
    if History(args.history).delete_profile_field(normalize(label)):
        print(f'[OK] Профиль очищен: "{label}".')
    else:
        print(f'[INFO] Ручное поле профиля не найдено: "{label}".')

"""Команда clear-skipped (#87): очистка журнала отсева вакансий.

``clear-skipped [--reason <r>] [--dry-run]`` — удаляет записи из таблицы
``skipped`` (кэш отсева filter_candidates, #87). Без браузера, только SQLite.

WRITE-local: меняет ТОЛЬКО локальную историю, на hh.ru ничего не отправляет.
Поэтому ``--force`` не требуется (в отличие от WRITE-hh-ru команд), но
``--dry-run`` поддерживается для проверки «сколько уйдёт» без удаления.

Вывод соответствует cli-spec §clear-skipped:
  ``[INFO] Найдено пропущенных (<reason|все>): N``
  ``[DRY-RUN] ничего не удалено.``  (если --dry-run)
  ``[OK] Удалено N записей пропусков (<reason|все>).``  (иначе)

Регистрируется автоматически через pkgutil.iter_modules (cli.py не трогается).
``--reason`` choices = стабильные enum-ключи из :data:`~hhru_bot.history.SKIP_REASON_VALUES`.
"""

from __future__ import annotations

import argparse

from ..history import SKIP_REASON_VALUES, History


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "clear-skipped",
        help="Очистить журнал пропущенных вакансий (кэш отсева)",
        description=(
            "Удалить записи из журнала отсева (таблица skipped). Без --reason "
            "чистит все причины. Без --dry-run удаляет, с --dry-run — только "
            "показывает, сколько записей ушло бы."
        ),
    )
    p.add_argument(
        "--reason",
        choices=SKIP_REASON_VALUES,
        default=None,
        help="Очистить только эту причину (по умолчанию — все причины)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, сколько записей будет удалено, без реального удаления",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Удаляет записи skipped, возвращает число удалённых (0 в dry-run).

    ``args.config`` не используется: clear-skipped не зависит от config.yaml
    (чистит локальную историю, без браузера). Аргумент всё равно есть в
    namespace — глобальные флаги cli добавляет всем командам.
    """
    history = History(args.history)
    reason = args.reason
    label = reason if reason else "все причины"

    found = history.count_skipped(reason)
    print(f"[INFO] Найдено пропущенных ({label}): {found}")

    if args.dry_run:
        print("[DRY-RUN] ничего не удалено.")
        return 0

    deleted = history.clear_skipped(reason)
    print(f"[OK] Удалено {deleted} записей пропусков ({label}).")
    return deleted

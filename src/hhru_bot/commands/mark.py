"""Команда mark (#13): ручная пометка оффера.

``mark --vacancy <id> --status offer`` — hh.ru не отдаёт оффер как статус
переговоров, поэтому верхний шаг воронки (оффер) заполняется вручную. Пишет
ЛИПСКУЮ пометку в отдельную таблицу manual_offers (НЕ в responses #12 — тот
перезаписывается каждым scrape'ом и затёр бы ручной offer).

Браузер НЕ нужен — только SQLite. Регистрируется автоматически через
pkgutil.iter_modules (cli.py не трогается).

--resume обязателен: ключ manual_offers = UNIQUE(resume_id, vacancy_id), пометка
привязана к конкретному резюме. Резолвится slug → resume.resume_id тем же ключом,
что и apply/bump/stats.
"""

from __future__ import annotations

import argparse
import sys

# Сейчас mark поддерживает только оффер — единственный «ручной» шаг воронки.
# Прочие статусы (read/invitation/discard) проставляет #12 из живых переговоров.
SUPPORTED_STATUSES = ("offer",)


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "mark",
        help="Ручная пометка статуса вакансии (напр. оффер, который hh.ru не отдаёт)",
    )
    p.add_argument("--resume", help="ID резюме из конфига (обязательно)")
    p.add_argument("--vacancy", help="ID вакансии (число из URL https://hh.ru/vacancy/<id>)")
    p.add_argument(
        "--status",
        choices=SUPPORTED_STATUSES,
        default="offer",
        help="Статус для пометки (по умолчанию offer)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..config import ConfigError, load_config_or_exit
    from ..history import History

    if args.resume is None:
        print("Ошибка: укажите --resume <id> (ключ истории — по резюме+вакансии).", file=sys.stderr)
        sys.exit(1)
    if not args.vacancy:
        print("Ошибка: укажите --vacancy <id>.", file=sys.stderr)
        sys.exit(1)
    # Runtime-проверка статуса: argparse choices защищает CLI-вызов, но run()
    # может вызываться напрямую (тесты/импорт) — defence-in-depth, как в др. командах.
    if args.status not in SUPPORTED_STATUSES:
        print(
            f"Ошибка: статус {args.status!r} не поддерживается. Допустимо: "
            f"{', '.join(SUPPORTED_STATUSES)}.",
            file=sys.stderr,
        )
        sys.exit(1)

    config = load_config_or_exit(args.config)
    try:
        resume = config.get_resume(args.resume)
    except ConfigError as e:
        print(f"Резюме не найдено: {e}", file=sys.stderr)
        sys.exit(1)

    history = History(args.history)
    created = history.mark_offer(args.vacancy, resume_id=resume.resume_id)
    if created:
        print(f"[OK] Вакансия {args.vacancy} помечена как {args.status} для резюме {resume.id}.")
    else:
        print(
            f"Вакансия {args.vacancy} уже имела статус {args.status} "
            f"для резюме {resume.id} (без изменений)."
        )

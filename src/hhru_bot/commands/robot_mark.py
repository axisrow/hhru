"""Команда robot-mark: вердикт пользователя «робот или человек» для чата.

Эвристики детекта (лейбл бота / 2+ вопроса / слишком быстрый ответ) —
доверчивые догадки: спроектированный под обман текст их обходит. Вердикт
человека хранится в robot_verdicts и в гейтах reply-employers стоит ВЫШЕ
эвистик — машина его не перезаписывает (снять можно только --clear).

Браузер НЕ нужен — только SQLite. Регистрируется автоматически через
pkgutil.iter_modules (cli.py не трогается).

Консистентность robot-queue поддерживается самой командой: --robot заводит
строку очереди (reason='user_verdict', vacancy_id неизвестен отметке —
живой sweep дозаполнит её не сможет, INSERT OR IGNORE хранит первую; для
очереди это только отображаемое поле), --human резолвит висящую строку —
чат возвращается в обычный план ответов.
"""

from __future__ import annotations

import argparse
import sys

_ACTIONS = ("robot", "human", "clear")


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "robot-mark",
        help="Вердикт пользователя: чат ведёт робот или человек (перекрывает эвристики)",
    )
    p.add_argument(
        "--topic",
        help="ID topic из robot-queue / probe --negotiations (обязательно)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--robot",
        action="store_true",
        help="Считать чат роботом: ручная очередь, без автоматических писем",
    )
    group.add_argument(
        "--human",
        action="store_true",
        help="Считать чат человеком: снять с robot-очереди, вернуть в план ответов",
    )
    group.add_argument(
        "--clear",
        action="store_true",
        help="Снять вердикт: эвристики снова решают сами",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..history import History

    if not args.topic:
        print("Ошибка: укажите --topic <id> (список — robot-queue).", file=sys.stderr)
        sys.exit(1)
    chosen = [action for action in _ACTIONS if getattr(args, action)]
    # argparse mutually_exclusive_group защищает CLI-вызов, но run() может
    # вызываться напрямую (тесты/импорт) — defence-in-depth, как в mark.py.
    if len(chosen) != 1:
        print(
            "Ошибка: укажите ровно одно действие: --robot, --human или --clear.",
            file=sys.stderr,
        )
        sys.exit(1)

    history = History(args.history)
    if args.robot:
        history.set_robot_verdict(args.topic, verdict="robot")
        # Идемпотентно: повторная отметка не плодит строк (topic UNIQUE).
        history.mark_robot_questionnaire(args.topic, reason="user_verdict")
        print(f"[OK] {args.topic}: вердикт — робот. Чат в ручной очереди (robot-queue).")
    elif args.human:
        history.set_robot_verdict(args.topic, verdict="human")
        row = history.robot_questionnaire_row(args.topic)
        if row is not None and row.get("resolved_at") is None:
            history.resolve_robot_questionnaire(
                args.topic, answer="reclassified human by robot-mark"
            )
            print(f"[OK] {args.topic}: вердикт — человек; строка robot-очереди снята.")
        else:
            print(f"[OK] {args.topic}: вердикт — человек (robot-очередь для topic пуста).")
    else:
        history.clear_robot_verdict(args.topic)
        print(f"[OK] {args.topic}: вердикт снят — эвристики снова решают сами.")

"""Команда robot-mark: вердикт пользователя «робот или человек» для чата.

Эвристики детекта (лейбл бота / 2+ вопроса / слишком быстрый ответ) —
доверчивые догадки: спроектированный под обман текст их обходит. Вердикт
человека хранится в robot_verdicts и в гейтах reply-employers стоит ВЫШЕ
эвистик — машина его не перезаписывает (снять можно только --clear).

Браузер НЕ нужен — только SQLite. Регистрируется автоматически через
pkgutil.iter_modules (cli.py не трогается).

Консистентность robot-queue поддерживается самой командой: --robot заводит
или ре-открывает строку очереди (reason='user_verdict', снятая резолюция
robot-reply сбрасывается), --human резолвит висящую строку — чат
возвращается в обычный план ответов; --unreachable резолвит строку БЕЗ
вердикта — чат недостижим (топик исчез из negotiations, #1154), и вердикт
о природе собеседника взять неоткуда.
"""

from __future__ import annotations

import argparse
import sys

_ACTIONS = ("robot", "human", "clear", "unreachable")

#: Аудит-значение answer при резолве без ответа (#1154): строка снимается с
#: очереди, но «отвечено: …» в robot-queue не должно читаться как текст ответа.
UNREACHABLE_ANSWER = "чат недостижим (топик отсутствует в negotiations)"


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
    group.add_argument(
        "--unreachable",
        action="store_true",
        help=(
            "Снять строку очереди БЕЗ вердикта: чат недостижим (топик исчез "
            "из negotiations — вакансия недоступна/переписка удалена, #1154). "
            "robot_verdicts не пишется"
        ),
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
        # Идемпотентно: повторная отметка не плодит строк (topic UNIQUE), а
        # уже резолвнутая строка (например, robot-reply ответил до вердикта)
        # ре-открывается — очередь снова показывает чат как ручной.
        history.reopen_robot_questionnaire(args.topic, reason="user_verdict")
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
    elif args.unreachable:
        # Резолв БЕЗ вердикта (#1154): недостижимый чат не «человек» и не
        # «робот» — вердикт разгейтит эвристики для topic, а знать о нём
        # нечего (читать чат нечем). Снимается только висящая строка очереди.
        row = history.robot_questionnaire_row(args.topic)
        if row is None:
            print(f"[OK] {args.topic}: robot-очередь для topic пуста.")
        elif row.get("resolved_at") is not None:
            print(f"[OK] {args.topic}: строка уже резолвнута ({row.get('resolved_at')}).")
        else:
            history.resolve_robot_questionnaire(args.topic, answer=UNREACHABLE_ANSWER)
            print(f"[OK] {args.topic}: строка robot-очереди снята без вердикта (чат недостижим).")
    else:
        history.clear_robot_verdict(args.topic)
        print(f"[OK] {args.topic}: вердикт снят — эвристики снова решают сами.")

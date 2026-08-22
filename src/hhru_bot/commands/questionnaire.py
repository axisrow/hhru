"""Управление обучаемыми шаблонами анкет (issue #482).

Git-style сабкоманды, по образцу `account.py`:

    hhru questionnaire pending [--resume ID]                                   READ
    hhru questionnaire templates [--resume ID]                                 READ
    hhru questionnaire learn [--resume ID] [--limit N]                         WRITE-local
    hhru questionnaire set TEMPLATE --mode static --answer VALUE [--resume ID] WRITE-local
    hhru questionnaire set TEMPLATE --mode contextual --instruction TEXT
        [--example TEXT ...] [--resume ID]                                    WRITE-local
    hhru questionnaire unset TEMPLATE [--resume ID]                           WRITE-local

`set`/`unset`/`learn` мутируют только `history.db` (шаблоны/ответы/pending) —
не hh.ru, поэтому используют общий локальный write lock (как `account create`),
а не тяжёлый `command_runs`-lease (тот — для durable hh.ru-мутаций с
`ApplyProgress`, см. `commands/_common.py::run_supervised_command`).

`learn` — интерактивная обработка pending-очереди: только если stdin — TTY.
В неинтерактивном запуске (headless/pipe) с непустой очередью команда
завершается `[FAIL]`, не подвисая на `input()` (issue #482: "Headless/non-TTY:
неизвестный вопрос идет в очередь, вакансия пропускается, batch продолжается" —
то же правило применяется и здесь, к самой команде learn).
"""

from __future__ import annotations

import argparse
import sys

from ..report import _ascii_table


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "questionnaire",
        help="Обучаемые шаблоны ответов на анкеты (keyword resolver, #482)",
        description="Просмотр и редактирование шаблонов ответов на вопросы анкет отклика.",
    )
    commands = p.add_subparsers(dest="questionnaire_command", required=True)

    pending = commands.add_parser("pending", help="Показать неотвеченные вопросы анкет (READ)")
    pending.add_argument("--resume", help="Ограничить одним резюме (slug или resume_id)")
    pending.set_defaults(func=run_pending)

    templates = commands.add_parser("templates", help="Показать сохранённые шаблоны (READ)")
    templates.set_defaults(func=run_templates)

    learn = commands.add_parser(
        "learn",
        help="Интерактивно разобрать очередь неотвеченных вопросов",
        description="Для каждого неотвеченного вопроса предлагает создать/подтвердить шаблон.",
    )
    learn.add_argument("--resume", help="Ограничить одним резюме (slug или resume_id)")
    learn.add_argument("--limit", type=int, default=20, help="Сколько вопросов обработать")
    learn.set_defaults(func=run_learn)

    set_cmd = commands.add_parser("set", help="Создать/обновить шаблон и его ответ")
    set_cmd.add_argument("template", help="Имя шаблона")
    set_cmd.add_argument(
        "--mode", choices=("static", "contextual"), required=True, help="Тип шаблона"
    )
    set_cmd.add_argument("--answer", help="Фиксированный ответ (только --mode static)")
    set_cmd.add_argument("--instruction", help="Инструкция для LLM (только --mode contextual)")
    set_cmd.add_argument(
        "--example",
        action="append",
        help="Подтверждённый пример ответа (можно несколько раз, только contextual)",
    )
    set_cmd.add_argument(
        "--resume", help="Сохранить ответ только для этого резюме (иначе — общий для аккаунта)"
    )
    set_cmd.set_defaults(func=run_set)

    unset = commands.add_parser("unset", help="Удалить шаблон целиком")
    unset.add_argument("template", help="Имя шаблона")
    unset.set_defaults(func=run_unset)


def _resolve_resume_id(args: argparse.Namespace) -> str | None:
    """Резолвит --resume (slug или hash) в resume_id, если задан."""
    resume_key = getattr(args, "resume", None)
    if resume_key is None:
        return None
    from ..config import ConfigError, load_config_or_exit

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    try:
        resume = resolve_resume(config, resume_key)
    except ConfigError as exc:
        raise SystemExit(f"[FAIL] {exc}") from exc
    return resume.resume_id


def run_pending(args: argparse.Namespace) -> None:
    from ..history import History

    resume_id = getattr(args, "resume", None)
    rows = History(args.history).list_pending(resume_id=resume_id)
    if not rows:
        print("[INFO] Очередь неотвеченных вопросов пуста.")
        return
    print(
        _ascii_table(
            ["id", "resume_id", "vacancy_id", "question_text", "suggested_template"],
            [
                [
                    str(row["id"]),
                    row["resume_id"],
                    row["vacancy_id"],
                    row["question_text"],
                    row["suggested_template"] or "",
                ]
                for row in rows
            ],
        )
    )


def run_templates(args: argparse.Namespace) -> None:
    from ..history import History

    templates = History(args.history).list_templates()
    if not templates:
        print("[INFO] Шаблонов пока нет. Используйте `questionnaire set`.")
        return
    print(
        _ascii_table(
            ["name", "mode", "instruction"],
            [[t.name, t.mode, t.instruction or ""] for t in templates],
        )
    )


def run_set(args: argparse.Namespace) -> bool:
    from ..history import History

    if args.mode == "static":
        if not args.answer:
            print("[FAIL] --mode static требует --answer")
            return True
        if args.instruction or args.example:
            print("[FAIL] --instruction/--example применимы только к --mode contextual")
            return True
    else:
        if not args.instruction:
            print("[FAIL] --mode contextual требует --instruction")
            return True
        if args.answer:
            print("[FAIL] --answer применим только к --mode static")
            return True

    resume_id = _resolve_resume_id(args)
    history = History(args.history)
    history.upsert_template(
        args.template,
        mode=args.mode,
        instruction=args.instruction,
        examples=args.example,
    )
    if args.mode == "static":
        history.set_template_answer(args.template, args.answer, resume_id=resume_id)
    scope = f"резюме '{resume_id}'" if resume_id else "аккаунта"
    print(f"[OK] Шаблон '{args.template}' ({args.mode}) сохранён для {scope}.")
    return False


def run_unset(args: argparse.Namespace) -> bool:
    from ..history import History

    history = History(args.history)
    if history.get_template(args.template) is None:
        print(f"[FAIL] Шаблон '{args.template}' не найден.")
        return True
    history.delete_template(args.template)
    print(f"[OK] Шаблон '{args.template}' удалён.")
    return False


def run_learn(args: argparse.Namespace) -> bool:
    from ..history import History

    resume_id = getattr(args, "resume", None)
    history = History(args.history)
    pending = history.list_pending(resume_id=resume_id)[: args.limit]
    if not pending:
        print("[INFO] Очередь неотвеченных вопросов пуста — учить нечего.")
        return False
    if not sys.stdin.isatty():
        print(
            "[FAIL] `questionnaire learn` требует интерактивного терминала "
            f"(в очереди {len(pending)} вопрос(ов) — используйте `questionnaire set` "
            "напрямую или запустите learn в интерактивном терминале)."
        )
        return True

    resolved = 0
    skipped = 0
    try:
        for item in pending:
            if _learn_one(history, item):
                resolved += 1
            else:
                skipped += 1
    except (EOFError, KeyboardInterrupt):
        print(f"\n[INFO] Прервано пользователем. Обработано: {resolved}, пропущено: {skipped}.")
        raise SystemExit(130) from None

    print(f"[OK] Обработано: {resolved}, пропущено: {skipped}.")
    return False


def _learn_one(history, item: dict) -> bool:
    """Интерактивно обрабатывает одну pending-запись. Возвращает True, если решена."""
    print(f"\nВопрос: {item['question_text']}")
    if item.get("suggested_template"):
        confidence = item.get("suggested_confidence")
        confidence_str = f"{confidence:.2f}" if confidence is not None else "?"
        print(
            f"Предложенное соответствие (LLM, confidence={confidence_str}): "
            f"{item['suggested_template']}"
        )
    print(
        "Действие: [c]onfirm предложенный шаблон / [n]ew шаблон / [s]kip: ",
        end="",
    )
    choice = input().strip().lower()
    if choice == "c" and item.get("suggested_template"):
        history.confirm_match(
            _normalize_for_match(item["question_text"]), item["suggested_template"]
        )
        history.resolve_pending(item["id"])
        print(f"[OK] Подтверждено: '{item['question_text']}' -> {item['suggested_template']}")
        return True
    if choice == "n":
        name = input("Имя нового шаблона: ").strip()
        if not name:
            print("[skip] Имя не задано.")
            return False
        mode = input("Тип (static/contextual) [static]: ").strip() or "static"
        if mode == "static":
            answer = input("Ответ: ").strip()
            if not answer:
                print("[skip] Ответ не задан.")
                return False
            history.upsert_template(name, mode="static")
            history.set_template_answer(name, answer)
        else:
            instruction = input("Инструкция: ").strip()
            if not instruction:
                print("[skip] Инструкция не задана.")
                return False
            history.upsert_template(name, mode="contextual", instruction=instruction)
        history.confirm_match(_normalize_for_match(item["question_text"]), name)
        history.resolve_pending(item["id"])
        print(f"[OK] Создан шаблон '{name}' и подтверждено сопоставление.")
        return True
    print("[skip] Пропущено.")
    return False


def _normalize_for_match(text: str) -> str:
    from ..external_forms.detect import normalize

    return normalize(text)

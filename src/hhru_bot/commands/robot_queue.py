"""Read-only listing of employer robot questionnaires."""

from __future__ import annotations


def register(subparsers) -> None:
    parser = subparsers.add_parser("robot-queue", help="Показать диалоги с анкетами-роботами")
    parser.add_argument("--limit", type=int, default=50)
    parser.set_defaults(func=run)


def run(args) -> None:
    from ..history import History

    if args.limit < 1:
        print("[FAIL] --limit должен быть >= 1")
        return
    history = History(args.history)
    rows = history.list_robot_questionnaires(args.limit)
    if not rows:
        print("[INFO] Очередь анкет-роботов пуста.")
        return
    pending = 0
    for row in rows:
        verdict = history.robot_verdict(row["topic"])
        verdict_note = (
            f" — вердикт: {'робот' if verdict == 'robot' else 'человек'}" if verdict else ""
        )
        if row.get("resolved_at"):
            answer = row.get("answer") or "?"
            print(
                f"{row['topic']} — вакансия {row['vacancy_id'] or '?'} — "
                f"{row['reason']} — отвечено: {answer}{verdict_note}"
            )
        else:
            pending += 1
            print(
                f"{row['topic']} — вакансия {row['vacancy_id'] or '?'} — "
                f"{row['reason']}{verdict_note}"
            )
    if pending:
        print(
            f"[INFO] Неотвеченных: {pending}. Ответить кнопкой быстрых ответов "
            '(Да/Нет): hhru robot-reply --topic <topic> --answer "Нет" --dry-run. '
            "Вердикт человека (перекрывает эвристики): "
            "hhru robot-mark --topic <topic> --robot|--human|--clear"
        )

"""Manage local questionnaire templates and the confirmation queue (#482)."""

from __future__ import annotations

import argparse
import json

from ..questionnaire_answers import (
    CLUSTERS,
    MODES,
    SCOPES,
    SEED_TEMPLATES,
    is_template_key,
    normalize_question,
    question_fingerprint,
    sync_seed_templates,
)
from ..report import _ascii_table


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "questionnaire", help="Шаблоны и очередь ответов на анкеты работодателей"
    )
    actions = parser.add_subparsers(dest="questionnaire_action", required=True)

    pending = actions.add_parser("pending", help="Показать неподтверждённые вопросы")
    pending.add_argument("--resume")

    templates = actions.add_parser("templates", help="Показать шаблоны и ответы")
    templates.add_argument("--resume")

    learn = actions.add_parser("learn", help="Интерактивно разобрать очередь вопросов")
    learn.add_argument("--resume")
    learn.add_argument("--limit", type=int, default=0)

    set_parser = actions.add_parser("set", help="Установить подтверждённый ответ")
    set_parser.add_argument("template")
    set_parser.add_argument("--resume")
    set_parser.add_argument("--mode", choices=sorted(MODES), required=True)
    set_parser.add_argument("--answer")
    set_parser.add_argument("--instruction")
    set_parser.add_argument("--example", action="append", default=[])

    unset = actions.add_parser("unset", help="Удалить подтверждённый ответ")
    unset.add_argument("template")
    unset.add_argument("--resume")
    parser.set_defaults(func=run)


def _history(args):
    from ..history import History

    history = History(args.history)
    return history


def _scope_id(args) -> str:
    return args.resume or ""


def _show_pending(history, resume_id: str | None, limit: int = 0) -> list[dict]:
    rows = history.list_questionnaire_pending(resume_id, limit=limit)
    print(
        _ascii_table(
            ["id", "resume", "vacancy", "question", "reason", "seen"],
            [
                [
                    row["id"],
                    row["resume_id"],
                    row["vacancy_id"] or "—",
                    row["raw_text"],
                    row["reason"],
                    row["seen_count"],
                ]
                for row in rows
            ],
        )
    )
    return rows


def _seed_learning_queue(history, resume_id: str | None) -> None:
    pending = {
        (row["fingerprint"], row["resume_id"])
        for row in history.list_questionnaire_pending(resume_id)
    }
    for row in history.list_questionnaire_questions_for_learning(resume_id):
        options = tuple(row["options"])
        fingerprint = question_fingerprint(row["text"], row["kind"], options)
        if history.get_questionnaire_alias(fingerprint) is not None:
            continue
        key = (fingerprint, row["resume_id"])
        if key in pending:
            continue
        history.enqueue_questionnaire_pending(
            fingerprint,
            row["resume_id"],
            row["vacancy_id"],
            row["text"],
            row["kind"],
            options,
            proposal=None,
            reason="исторический вопрос без подтверждённого шаблона",
        )
        pending.add(key)


def _template_from_user(history, row: dict, proposal: dict | None) -> dict | None:
    if proposal:
        print("[INFO] LLM-предложение: " + json.dumps(proposal, ensure_ascii=False))
    while True:
        action = (
            input("Принять [y], сопоставить [m], новый [n], пропустить [s], выйти [q]? ")
            .strip()
            .lower()
        )
        if action == "q":
            raise KeyboardInterrupt
        if action == "s":
            history.mark_questionnaire_pending(row["id"], "skipped")
            return None
        if action == "y" and proposal:
            key = proposal.get("template_key")
            existing = history.get_questionnaire_template(key) if isinstance(key, str) else None
            if existing is not None:
                return existing
            label = proposal.get("label")
            cluster = proposal.get("cluster")
            mode = proposal.get("mode")
            scope = proposal.get("scope")
            if (
                isinstance(key, str)
                and isinstance(label, str)
                and cluster in CLUSTERS
                and mode in MODES
                and scope in SCOPES
                and not (
                    (bool(proposal.get("sensitive")) or cluster == "compliance")
                    and mode != "static"
                )
            ):
                history.upsert_questionnaire_template(
                    key,
                    label,
                    cluster,
                    mode,
                    scope,
                    instruction=str(proposal.get("instruction") or ""),
                    sensitive=bool(proposal.get("sensitive")) or cluster == "compliance",
                    source="user",
                    confirmed=True,
                )
                return history.get_questionnaire_template(key)
            print("[FAIL] LLM-предложение не прошло валидацию")
            continue
        if action == "m":
            key = input("Ключ существующего шаблона: ").strip()
            template = history.get_questionnaire_template(key)
            if template is not None:
                return template
            print("[FAIL] Шаблон не найден")
            continue
        if action == "n":
            key = input("Ключ нового шаблона: ").strip()
            label = input("Название: ").strip()
            cluster = input("Кластер: ").strip()
            mode = input("Режим static/contextual: ").strip()
            scope = input("Область account/resume: ").strip()
            if (
                not is_template_key(key)
                or not label
                or cluster not in CLUSTERS
                or mode not in MODES
                or scope not in SCOPES
                or (cluster == "compliance" and mode != "static")
            ):
                print("[FAIL] Некорректные параметры шаблона")
                continue
            history.upsert_questionnaire_template(
                key,
                label,
                cluster,
                mode,
                scope,
                sensitive=cluster == "compliance",
                source="user",
                confirmed=True,
            )
            return history.get_questionnaire_template(key)
        print("[INFO] Выберите y/m/n/s/q")


def _confirmed_payload(row: dict, template: dict, proposal: dict | None) -> dict[str, object]:
    answer = proposal.get("answer") if proposal else None
    proposed_text = answer.get("text") if isinstance(answer, dict) else ""
    proposed_choices = answer.get("choices") if isinstance(answer, dict) else []
    options = row["options"]
    if template["default_mode"] == "static":
        if row["kind"] == "choice":
            print("Варианты: " + " | ".join(options))
            default = ", ".join(proposed_choices) if isinstance(proposed_choices, list) else ""
            raw = input(f"Ответы через запятую [{default}]: ").strip() or default
            choices = [item.strip() for item in raw.split(",") if item.strip()]
            return {"text": ", ".join(choices), "choices": choices}
        text = input(f"Ответ [{proposed_text or ''}]: ").strip() or str(proposed_text or "")
        return {"text": text, "choices": []}
    instruction = input(f"Инструкция [{template.get('instruction') or ''}]: ").strip()
    instruction = instruction or template.get("instruction") or "Сформируй правдивый краткий ответ."
    example = input(f"Пример ответа [{proposed_text or ''}]: ").strip() or str(proposed_text or "")
    return {"instruction": instruction, "examples": [example] if example else []}


def _learn(history, resume_id: str | None, limit: int) -> None:
    _seed_learning_queue(history, resume_id)
    rows = history.list_questionnaire_pending(resume_id, limit=limit)
    for row in rows:
        print(f"[INFO] Вопрос #{row['id']} ({row['resume_id']}): {row['raw_text']}")
        if row["options"]:
            print("[INFO] Варианты: " + " | ".join(row["options"]))
        template = _template_from_user(history, row, row["proposal"])
        if template is None:
            continue
        payload = _confirmed_payload(row, template, row["proposal"])
        scope_id = row["resume_id"] if template["default_scope"] == "resume" else ""
        history.set_questionnaire_answer(
            template["template_key"],
            scope_id=scope_id,
            mode=template["default_mode"],
            payload=payload,
            source="user",
            confirmed=True,
        )
        history.upsert_questionnaire_alias(
            row["fingerprint"],
            row["raw_text"],
            normalize_question(row["raw_text"]),
            row["kind"],
            row["options"],
            template["template_key"],
            template["cluster"],
            source="user",
            confirmed=True,
        )
        history.mark_questionnaire_pending(row["id"], "confirmed")
        print(f"[OK] Сохранён шаблон {template['template_key']}")


def run(args: argparse.Namespace) -> bool:
    history = _history(args)
    action = args.questionnaire_action
    if action in {"learn", "set", "unset"}:
        sync_seed_templates(history)
    if action == "pending":
        _show_pending(history, args.resume)
        return False
    if action == "templates":
        answers = {
            (row["template_key"], row["scope_id"]): row
            for row in history.list_questionnaire_answers(args.resume)
        }
        rows = []
        templates = {row["template_key"]: row for row in history.list_questionnaire_templates()}
        for seed in SEED_TEMPLATES:
            templates.setdefault(
                seed.key,
                {
                    "template_key": seed.key,
                    "cluster": seed.cluster,
                    "default_mode": seed.default_mode,
                    "default_scope": seed.default_scope,
                },
            )
        for template in sorted(
            templates.values(), key=lambda row: (row["cluster"], row["template_key"])
        ):
            scopes = [""] + ([args.resume] if args.resume else [])
            answer = next(
                (
                    answers[(template["template_key"], scope)]
                    for scope in reversed(scopes)
                    if (template["template_key"], scope) in answers
                ),
                None,
            )
            rows.append(
                [
                    template["template_key"],
                    template["cluster"],
                    template["default_mode"],
                    template["default_scope"],
                    json.dumps(answer["payload"], ensure_ascii=False) if answer else "—",
                ]
            )
        print(_ascii_table(["template", "cluster", "mode", "scope", "answer"], rows))
        return False
    if action == "learn":
        if args.limit < 0:
            print("[FAIL] --limit не может быть отрицательным")
            return True
        _learn(history, args.resume, args.limit)
        return False
    if action == "set":
        template = history.get_questionnaire_template(args.template)
        if template is None:
            print(f"[FAIL] Шаблон не найден: {args.template}")
            return True
        if template["sensitive"] and args.mode != "static":
            print("[FAIL] Чувствительный шаблон поддерживает только mode=static")
            return True
        if args.mode == "static":
            if not args.answer:
                print("[FAIL] Для static требуется --answer")
                return True
            payload = {"text": args.answer, "choices": []}
        else:
            if not args.instruction:
                print("[FAIL] Для contextual требуется --instruction")
                return True
            payload = {"instruction": args.instruction, "examples": args.example}
        history.set_questionnaire_answer(
            args.template,
            scope_id=_scope_id(args),
            mode=args.mode,
            payload=payload,
            source="user",
            confirmed=True,
        )
        print(f"[OK] Ответ сохранён: {args.template}")
        return False
    if history.delete_questionnaire_answer(args.template, _scope_id(args)):
        print(f"[OK] Ответ удалён: {args.template}")
    else:
        print(f"[INFO] Ответ не найден: {args.template}")
    return False

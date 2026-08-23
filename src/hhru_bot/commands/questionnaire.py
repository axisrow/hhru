"""Управление обучаемыми шаблонами ответов на анкеты (#482).

Команда локальная: браузер не открывается, на hh.ru ничего не отправляется.
Шаблоны, подтверждённые формулировки и очередь неотвеченных вопросов живут в
``history.db`` рядом с остальной историей аккаунта.

Скоуп задаётся флагом ``--resume``: без него правится общий ответ аккаунта, с
ним — переопределение для конкретного резюме, которое имеет приоритет
(тот же приём, что manual над hh_ru в ``profile``).
"""

from __future__ import annotations

import argparse
import sys

# Импорт на уровне модуля (а не ленивый, как обычно в commands/): значения нужны
# для choices ещё при построении парсера. Пакет questionnaires намеренно лёгкий
# — чистые данные, без Playwright и без optional-зависимости .[ai].
from ..questionnaires.templates import CLUSTERS, MODES


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "questionnaire",
        help="Шаблоны ответов на анкеты работодателей",
        description="Показать очередь, аудит и шаблоны, задать ответ или обучить шаблон.",
    )
    commands = parser.add_subparsers(dest="questionnaire_command", required=True)

    pending = commands.add_parser(
        "pending",
        help="Показать вопросы анкет, ожидающие решения (READ)",
        description="Вопросы, на которые бот не стал отвечать сам.",
    )
    pending.add_argument("--resume", help="Slug резюме или resume_id (по умолчанию — все)")
    pending.add_argument("--limit", type=int, default=50, help="Сколько строк вывести")
    pending.set_defaults(func=run_pending)

    templates = commands.add_parser(
        "templates",
        help="Показать сохранённые шаблоны ответов (READ)",
        description="Шаблоны уровня аккаунта и переопределения резюме.",
    )
    templates.add_argument("--resume", help="Slug резюме или resume_id")
    templates.set_defaults(func=run_templates)

    audit = commands.add_parser(
        "audit",
        help="Показать сохранённые ответы на анкеты (READ)",
        description="Что бот ответил в анкетах: ответ, уверенность, шаблон.",
    )
    audit.add_argument("--resume", help="Slug резюме или resume_id (по умолчанию — все)")
    # dest="limit", чтобы переиспользовать _limit() — он уже охраняет от
    # `LIMIT -1` («без ограничения» в SQLite), а своя проверка вернула бы
    # ровно тот дефект, ради которого хелпер и написан.
    audit.add_argument(
        "--last", dest="limit", type=int, default=50, help="Сколько последних ответов показать"
    )
    audit.add_argument("--template", help="Только ответы этого шаблона")
    audit.add_argument(
        "--low-confidence",
        action="store_true",
        # Причину «низкая уверенность» база не хранит — в ней записано только
        # решение не сохранять ответ, а к нему приводит и отказ compliance-гейта.
        help="Только вопросы, на которые бот не стал отвечать",
    )
    audit.set_defaults(func=run_audit)

    learn = commands.add_parser(
        "learn",
        help="Разобрать очередь и задать ответы (WRITE-local)",
        description="Интерактивный разбор накопившихся вопросов анкет.",
    )
    learn.add_argument("--resume", help="Slug резюме или resume_id")
    learn.add_argument("--limit", type=int, default=20, help="Сколько вопросов разобрать")
    learn.set_defaults(func=run_learn)

    set_parser = commands.add_parser(
        "set",
        help="Задать ответ для шаблона (WRITE-local)",
        description="static — готовое значение; contextual — инструкция для LLM.",
    )
    set_parser.add_argument("template", help="Имя шаблона, например salary")
    set_parser.add_argument(
        "--mode", choices=MODES, required=True, help="static (значение) или contextual (инструкция)"
    )
    set_parser.add_argument("--answer", help="Готовый ответ (для --mode static)")
    set_parser.add_argument("--instruction", help="Инструкция для LLM (для --mode contextual)")
    set_parser.add_argument(
        "--example",
        action="append",
        default=[],
        metavar="TEXT",
        help="Формулировка вопроса, относящаяся к этому шаблону (можно повторять)",
    )
    set_parser.add_argument("--cluster", choices=CLUSTERS, help="Тематический кластер вопроса")
    set_parser.add_argument("--resume", help="Задать только для этого резюме")
    set_parser.set_defaults(func=run_set)

    unset_parser = commands.add_parser(
        "unset",
        help="Удалить шаблон (WRITE-local)",
        description="Удаляет шаблон только из своего скоупа.",
    )
    unset_parser.add_argument("template", help="Имя шаблона")
    unset_parser.add_argument("--resume", help="Снять только переопределение этого резюме")
    unset_parser.set_defaults(func=run_unset)


def _limit(args: argparse.Namespace) -> int:
    """Проверенный ``--limit``. Отрицательное значение — явная ошибка.

    В SQLite ``LIMIT -1`` означает «без ограничения», то есть опечатка вроде
    ``--limit -5`` давала бы поведение, прямо противоположное намерению, и
    молча: команда напечатала бы ВСЮ очередь.
    """
    limit = getattr(args, "limit", 0) or 0
    if limit < 1:
        print("[FAIL] --limit должен быть >= 1", file=sys.stderr)
        sys.exit(1)
    return limit


def _scope(args: argparse.Namespace) -> str | None:
    """Ключ хранения: реальный resume_id HH.ru, а не slug из конфига.

    Slug — локальное имя записи в config.yaml, он может быть переименован; вся
    остальная история (actions/skipped/questionnaire_scans) ключуется реальным
    resume_id, и шаблоны обязаны использовать тот же ключ, иначе
    переопределение «потерялось бы» после переименования резюме в конфиге.
    Резолв через конфиг, но без падения: работать с шаблонами можно и по сырому
    resume_id, когда конфига под рукой нет.
    """
    if not args.resume:
        return None
    from ..config import ConfigError, load_config

    try:
        config = load_config(args.config)
    except (ConfigError, SystemExit, OSError):
        return args.resume
    try:
        return config.get_resume(args.resume).resume_id
    except ConfigError:
        return args.resume


def run_pending(args: argparse.Namespace) -> None:
    from ..history import History
    from ..report import _ascii_table

    limit = _limit(args)
    scope = _scope(args)
    history = History(args.history)
    rows = history.list_questionnaire_pending(scope, limit=limit)
    # Вопросы из ранее собранных сканов только СЧИТАЮТСЯ, но не записываются:
    # pending классифицирована READ (cli._is_write_command), не берёт общий
    # write-lock и обязана оставаться доступной во время идущего apply. Запись
    # делает learn — она WRITE-local и лок держит.
    unseeded = sum(len(items) for items in _unqueued_scanned(history, scope).values())
    if not rows:
        print("[INFO] Очередь вопросов анкет пуста.")
        if unseeded:
            print(
                f"[INFO] В собранных анкетах есть неразобранных вопросов: {unseeded}. "
                "Добавить в очередь и разобрать: hhru questionnaire learn"
            )
        return
    print(
        _ascii_table(
            ["id", "resume", "вопрос", "шаблон", "причина"],
            [
                [
                    str(row["id"]),
                    row["resume_id"],
                    row["question_text"],
                    row["template"] or "-",
                    row["reason"],
                ]
                for row in rows
            ],
        )
    )
    print(f"[INFO] Ожидает решения: {len(rows)}. Разобрать: hhru questionnaire learn")
    if unseeded:
        print(f"[INFO] Ещё не в очереди, из собранных анкет: {unseeded}.")


def run_templates(args: argparse.Namespace) -> None:
    from ..history import History
    from ..report import _ascii_table

    rows = History(args.history).list_questionnaire_templates(_scope(args))
    if not rows:
        print("[INFO] Шаблоны ответов не заданы.")
        return
    print(
        _ascii_table(
            ["шаблон", "скоуп", "кластер", "режим", "ответ/инструкция"],
            [
                [
                    row["template"],
                    row["resume_id"] or "account",
                    row["cluster"],
                    row["mode"],
                    (row["answer"] if row["mode"] == "static" else row["instruction"]) or "",
                ]
                for row in rows
            ],
        )
    )


#: Ширина текстовых колонок аудита. Обрезка живёт здесь, а не в
#: ``report._ascii_table`` и не в ``History``: таблица рисуется по фактической
#: ширине ячеек, и один вопрос на 300 символов растянул бы её до нечитаемости,
#: а метод истории обязан отдавать значения целиком — по ним пишутся тесты.
_AUDIT_TEXT_WIDTH = 48


def _clip(text: str | None, width: int = _AUDIT_TEXT_WIDTH) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 3] + "..."


def _was_filled(row: dict) -> bool:
    """Попал ли ответ в форму вообще.

    ``filled`` батчевый — ``pipeline.py:589`` пишет его всему скану сразу, — и
    именно поэтому он верен ЗДЕСЬ и неверен как признак «низкой уверенности»:
    вопрос «заполнялась ли форма» тоже решается на весь скан. При единственном
    неуверенном вопросе вакансия отсеивается (``pipeline.py:615``), и уверенный
    сосед с непустым ``answer`` в форму не попадает.

    Признак отвечает ровно на этот вопрос и ни на какой более сильный:
    доставку на hh.ru он не подтверждает (см. ``_answer_cell``).
    """
    return bool(row["filled"])


def _answer_cell(row: dict) -> str:
    """Ячейка ответа: заполненное, отказ отвечать и незаполненное — разные факты.

    ГРАНИЦА ТОЧНОСТИ, и она же — причина формулировок. ``filled`` фиксирует
    успешное ЗАПОЛНЕНИЕ формы, но НЕ подтверждённую отправку на hh.ru (инвариант
    ``history.py``): он пишется на ``pipeline.py:628`` ДО анти-бот проверки,
    резервирования действия, письма и submit-клика (``:645``). Поэтому команда
    говорит только про заполнение формы и нигде не употребляет «отправлено»:
    заполненная форма могла не доехать (анти-бот, сбой submit, ``uncertain``), и
    подпись «отправлено» удостоверяла бы доставку чувствительных ответов
    работодателю, не имея тому доказательства. Исход submit лежит в ``actions``
    и сюда не джойнится — различать доставку #488 не берётся.

    Два маркера намеренно НЕ однокоренные: «форма не заполнялась» и «без
    ответа» — разные состояния, и общий корень делал бы их неразличимыми
    беглым взглядом по колонке.
    """
    if not _was_filled(row):
        # Предложение, которое в форму никогда не попадало. Строку не прячем —
        # её всё ещё оценивают, — но выдавать за заполненный ответ нельзя.
        return "[форма не заполнялась]"
    # Пустой ответ — не «ответили пустотой», а осознанный отказ отвечать
    # внутри заполненной формы: показываем это словами, а не пустой ячейкой.
    return _clip(row["answer"]) if row["answer"] else "[без ответа]"


def run_audit(args: argparse.Namespace) -> None:
    from ..history import History
    from ..report import _ascii_table

    rows = History(args.history).list_questionnaire_audit(
        _scope(args),
        template=args.template,
        low_confidence=args.low_confidence,
        limit=_limit(args),
    )
    if not rows:
        # «Аудит пуст» и «под фильтр ничего не попало» — разные факты. Слить их
        # в одну строку значит отправить оператора обратно в прямой SQL, ровно
        # от которого команда и избавляет.
        # Признак тот же, по которому фильтр ставится в SQL (``is not None``), а
        # не truthiness: `--template ""` — фильтр реальный, совпадений у него
        # просто нет, и сообщение «ответов нет» было бы ложью про базу.
        if args.template is not None or args.low_confidence or args.resume:
            print("[INFO] Под заданный фильтр не попало ни одного ответа.")
        else:
            print("[INFO] Сохранённых ответов на анкеты нет.")
        return
    print(
        _ascii_table(
            ["вакансия", "conf", "источник", "шаблон", "вопрос", "ответ"],
            [
                [
                    row["vacancy_id"],
                    # Уверенность печатается, но фильтровать по её порогу
                    # нечем — порог в базу не пишется (см. list_questionnaire_audit).
                    "-" if row["confidence"] is None else f"{row['confidence']:.2f}",
                    row["resolver_source"] or row["answer_source"] or "-",
                    row["template"] or "-",
                    _clip(row["text"]),
                    _answer_cell(row),
                ]
                for row in rows
            ],
        )
    )
    # Считаем ЗАПОЛНЕННОЕ, а не сохранённое: иначе счётчик повторил бы ту же
    # ложь, что и ячейка, этажом ниже. Про доставку счётчик молчит по той же
    # причине, что и ячейка, — ``filled`` её не доказывает (см. _answer_cell).
    answered = sum(1 for row in rows if _was_filled(row) and row["answer"])
    not_a_form = sum(1 for row in rows if not _was_filled(row))
    unanswered = sum(1 for row in rows if _was_filled(row) and not row["answer"])
    print(
        f"[INFO] Заполнено ответов: {answered}, без ответа: {unanswered}, "
        f"форма не заполнялась: {not_a_form}."
    )


def run_set(args: argparse.Namespace) -> None:
    from ..history import History
    from ..questionnaires.templates import (
        DEFAULT_CLUSTER,
        QuestionTemplate,
        TemplateError,
        cluster_for,
    )

    cluster = args.cluster or cluster_for(args.template) or DEFAULT_CLUSTER
    template = QuestionTemplate(
        name=args.template,
        cluster=cluster,
        mode=args.mode,
        answer=args.answer,
        instruction=args.instruction,
    )
    try:
        template.validate()
    except TemplateError as exc:
        # Проверка здесь, а не через mutually-exclusive group argparse: нужна
        # понятная строка [FAIL] и код возврата 1, а не argparse-usage.
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
    if args.example and args.mode != "contextual":
        print("[FAIL] --example имеет смысл только для --mode contextual", file=sys.stderr)
        sys.exit(1)
    from ..questionnaires.templates import is_strict

    if is_strict(cluster) and args.mode != "static":
        # Отказ на входе, а не при ответе: contextual-шаблон в строгом кластере
        # заведомо неисполним (compliance_gate его отвергнет), и сохранить его
        # значит дать оператору ложное ощущение настроенного ответа.
        print(
            f"[FAIL] кластер '{cluster}' допускает только --mode static: "
            "документы и комплаенс отвечаются явным сохранённым значением",
            file=sys.stderr,
        )
        sys.exit(1)

    scope = _scope(args)
    history = History(args.history)
    history.set_questionnaire_template(
        args.template,
        mode=args.mode,
        cluster=cluster,
        answer=args.answer,
        instruction=args.instruction,
        resume_id=scope,
    )
    for example in args.example:
        history.confirm_questionnaire_example(
            args.template, example, resume_id=scope, confirmed_by="seed"
        )
    where = f"резюме {scope}" if scope else "аккаунта"
    print(f"[OK] Шаблон '{args.template}' ({args.mode}, {cluster}) сохранён для {where}.")
    # Вопросы, стоявшие в очереди с пометкой «шаблон найден, ответа нет»,
    # теперь решены — иначе они остались бы висеть и держали бы свои вакансии
    # заблокированными, хотя ответ уже задан. Только для static: contextual без
    # настроенного LLM по-прежнему неисполним, снимать его с очереди рано.
    if args.mode == "static":
        history.resolve_pending_for_templates({args.template}, resume_id=scope)
    _unblock(history, scope)


def run_unset(args: argparse.Namespace) -> None:
    from ..history import History

    scope = _scope(args)
    if History(args.history).unset_questionnaire_template(args.template, resume_id=scope):
        where = f"резюме {scope}" if scope else "аккаунта"
        print(f"[OK] Шаблон '{args.template}' удалён для {where}.")
    else:
        print(f"[INFO] Шаблон '{args.template}' не найден в этом скоупе.")


def run_learn(args: argparse.Namespace):
    """Интерактивный разбор очереди: вопрос -> шаблон -> ответ.

    Не-TTY завершается сообщением, а не зависанием на stdin: команда штатно
    запускается человеком, но может попасть в cron или в пайп.
    """
    from ..exit_codes import CommandExitCode
    from ..history import History

    if not sys.stdin.isatty():
        print("[INFO] Неинтерактивный режим — обучение пропущено.")
        return None

    scope = _scope(args)
    history = History(args.history)
    if seeded := _seed_queue_from_scans(history, scope):
        print(f"[INFO] Добавлено в очередь из ранее собранных анкет: {seeded}.")
    rows = history.list_questionnaire_pending(scope, limit=_limit(args))
    if not rows:
        print("[INFO] Очередь вопросов анкет пуста.")
        return None

    learned = 0
    try:
        for row in rows:
            learned += _learn_one(history, row, scope)
    except KeyboardInterrupt:
        # Ctrl-C после нескольких разобранных вопросов не должен терять их:
        # каждый ответ уже записан, печатаем итог и отдаём общий код 130.
        print(f"\n[INFO] Прервано. Разобрано вопросов: {learned}.")
        _unblock(history, scope)
        return CommandExitCode.SIGINT

    print(f"[OK] Разобрано вопросов: {learned} из {len(rows)}.")
    _unblock(history, scope)
    return None


def _unqueued_scanned(history, scope: str | None) -> dict[str, list[dict]]:
    """Вопросы из собранных сканов, которых нет ни в очереди, ни в шаблонах.

    ЧИСТОЕ ЧТЕНИЕ — вызывается в том числе из READ-команды ``pending``, которая
    не берёт общий write-lock и потому не имеет права ничего писать.

    ``probe --questionnaires-only`` (#456) уже сложил в базу сотню реальных
    вопросов; без этого источника ``learn`` был бы пуст до первого боевого
    ``apply``, хотя материал давно собран. Вопросы, на которые резолвер и так
    отвечает, сюда не попадают — очередь для того, что бот решить не может.
    """
    import json

    from ..external_forms.detect import normalize
    from ..questionnaires.resolver import resolve_template

    templates = history.get_questionnaire_templates(scope)
    phrases = history.get_confirmed_phrases(scope)
    known = {row["question_key"] for row in history.list_questionnaire_pending(scope)}

    by_resume: dict[str, list[dict]] = {}
    for row in history.list_scanned_questions(scope):
        if normalize(row["text"]) in known:
            continue
        match = resolve_template(row["text"], confirmed=phrases)
        if match is not None and match.template in templates:
            continue
        by_resume.setdefault(row["resume_id"], []).append(
            {
                "text": row["text"],
                "kind": row["kind"],
                "is_radio": bool(row["is_radio"]),
                "options": json.loads(row["options_json"] or "[]"),
                "template": match.template if match else None,
                "cluster": match.cluster if match else None,
                "reason": "вопрос из собранных анкет, ответ не задан",
            }
        )
    return by_resume


def _seed_queue_from_scans(history, scope: str | None) -> int:
    """Записать в очередь вопросы из собранных сканов. Только для WRITE-команд.

    Заодно снимает строки, на которые ответ уже задан (static-шаблон появился
    после того, как вопрос туда попал): иначе они висели бы вечно, держа свои
    вакансии заблокированными. Contextual-шаблоны не снимаются — без
    настроенного LLM они по-прежнему неисполнимы.
    """
    templates = history.get_questionnaire_templates(scope)
    answerable = {
        name
        for name, row in templates.items()
        if row.get("mode") == "static" and (row.get("answer") or "").strip()
    }
    history.resolve_pending_for_templates(answerable, resume_id=scope)

    seeded = 0
    for resume_id, items in _unqueued_scanned(history, scope).items():
        if history.record_questionnaire_pending(resume_id, items):
            seeded += len(items)
    return seeded


def _learn_one(history, row: dict, scope: str | None) -> int:
    """Разобрать один вопрос очереди. Возвращает 1, если ответ задан."""
    import json

    print()
    print(f"Вопрос: {row['question_text']}")
    if options := json.loads(row["options_json"] or "[]"):
        kind = "один вариант" if row["is_radio"] else "один или несколько"
        print(f"  Варианты ({kind}): {' | '.join(options)}")
    print(f"  Причина: {row['reason']}")

    suggested = row["template"] or ""
    prompt = f"  Шаблон [{suggested}]: " if suggested else "  Шаблон (пусто — пропустить): "
    template = input(prompt).strip() or suggested
    if not template:
        print("  [skip] Вопрос оставлен в очереди.")
        return 0
    answer = input("  Ответ (пусто — пропустить): ").strip()
    if not answer:
        print("  [skip] Вопрос оставлен в очереди.")
        return 0

    from ..questionnaires.templates import DEFAULT_CLUSTER, cluster_for

    history.set_questionnaire_template(
        template,
        mode="static",
        cluster=row["cluster"] or cluster_for(template) or DEFAULT_CLUSTER,
        answer=answer,
        resume_id=scope,
    )
    history.confirm_questionnaire_example(
        template, row["question_text"], resume_id=scope, confirmed_by="user"
    )
    history.resolve_questionnaire_pending(row["id"])
    print(f"  [OK] Шаблон '{template}' сохранён.")
    return 1


def _unblock(history, scope: str | None) -> None:
    """Вернуть в оборот вакансии, пропущенные только из-за очереди анкет (#482).

    Вакансия была отсеяна потому, что бот не знал ответа; теперь знает, и
    держать её в журнале ``skipped`` — значит навсегда потерять её без ручного
    ``clear-skipped``. Снимаются только записи с причиной
    ``questionnaire_pending``; прочие основания отсева не трогаются.
    """
    if unblocked := history.clear_pending_skips(scope):
        print(f"[INFO] Возвращено в поиск вакансий: {unblocked}.")

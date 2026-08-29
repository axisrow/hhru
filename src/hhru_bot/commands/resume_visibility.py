"""Команда resume-visibility: изменить видимость резюме и стоп-лист (#566, #746)."""

from __future__ import annotations

import argparse

from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command
from .copy_resume import confirm_write

VISIBILITY_MODES = ("everyone", "no-one", "link-only", "whitelist", "blacklist")
_EMPLOYER_LIST_MODES = {"whitelist", "blacklist"}


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "resume-visibility",
        help="Изменить видимость резюме и стоп-лист работодателей на hh.ru",
        description=(
            "Изменяет режим видимости резюме и/или список работодателей "
            "whitelist/blacklist ('Кто видит'/'Кто не видит'). WRITE-hh.ru опасного "
            "уровня: боевой режим требует --force или подтверждения; --dry-run ничего "
            "не сохраняет. --resume all применяет одно и то же действие ко всем резюме "
            "аккаунта (основной сценарий: стоп-лист обычно общий для всех резюме)."
        ),
    )
    parser.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига, resume_id HH.ru или 'all' — все резюме аккаунта",
    )
    parser.add_argument(
        "--mode",
        choices=VISIBILITY_MODES,
        default=None,
        help="Новый режим видимости; без флага — режим не меняется, редактируется только список",
    )
    parser.add_argument(
        "--add-employer",
        action="append",
        default=[],
        metavar="NAME",
        dest="add_employer",
        help="Добавить работодателя в активный whitelist/blacklist (можно повторять)",
    )
    parser.add_argument(
        "--remove-employer",
        action="append",
        default=[],
        metavar="NAME",
        dest="remove_employer",
        help="Убрать работодателя из активного whitelist/blacklist (можно повторять)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать план без записи")
    parser.add_argument("--force", action="store_true", help="Подтвердить боевую запись")
    parser.set_defaults(func=run)


def _print_ambiguous(query: str, candidates) -> None:
    print(f"[FAIL] найдено несколько работодателей с именем «{query}» — уточните точное имя:")
    for candidate in candidates:
        city = f", {candidate.city}" if candidate.city else ""
        print(f"  - {candidate.name}{city} (employer_id={candidate.employer_id})")


def _resolve_resumes(config, key: str):
    from ._common import resolve_resume

    if key == "all":
        return list(config.resumes)
    return [resolve_resume(config, key)]


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..history import History
    from ..resume_visibility import set_resume_visibility_on_hh

    config = load_config_or_exit(args.config)
    add_employers = tuple(args.add_employer)
    remove_employers = tuple(args.remove_employer)

    if args.mode is None and not add_employers and not remove_employers:
        print("[FAIL] Укажите --mode и/или --add-employer/--remove-employer.")
        return True
    # #746 review round 2: эта проверка — только ранний отказ от явно несовместимого
    # --mode (например --mode everyone --add-employer X), общего для ВСЕХ резюме
    # запуска (в т.ч. --resume all). Она НЕ гарантирует, что список применится к
    # каждому резюме — при --mode=None у разных резюме аккаунта может быть разный
    # активный режим на hh.ru; окончательная per-resume проверка (чтение checked
    # radio) — в set_resume_visibility_on_hh (resume_visibility.py), она и есть
    # настоящий fail-closed барьер. Defense-in-depth: два уровня, не дублирование.
    if (add_employers or remove_employers) and args.mode not in (None, *(_EMPLOYER_LIST_MODES)):
        print(
            f"[FAIL] --add-employer/--remove-employer требуют --mode whitelist или "
            f"blacklist (или отсутствия --mode, если этот режим уже активен на hh.ru); "
            f"получен --mode {args.mode}."
        )
        return True

    try:
        resumes = _resolve_resumes(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True
    if not resumes:
        print("[FAIL] В конфиге нет ни одного резюме.")
        return True

    plan_bits = []
    if args.mode is not None:
        plan_bits.append(f"режим -> «{args.mode}»")
    for name in add_employers:
        plan_bits.append(f"добавить «{name}»")
    for name in remove_employers:
        plan_bits.append(f"убрать «{name}»")
    # #746 review: --resume all затрагивает несколько резюме одним подтверждением —
    # prompt обязан перечислить их поимённо, иначе "да" на batch-действие визуально
    # неотличимо от "да" на одно резюме (пользователь мог не осознавать масштаб).
    target = ", ".join(r.id for r in resumes) if args.resume == "all" else resumes[0].id
    prompt = f"Изменить видимость резюме [{target}] на hh.ru ({'; '.join(plan_bits)})?"
    if not args.dry_run and not confirm_write(args.force, prompt=prompt):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не изменено."
        )
        return True

    history = History(args.history)
    action = "resume_visibility"
    if not args.dry_run:
        blocked = [r.id for r in resumes if history.has_unresolved_uncertain(r.resume_id, action)]
        if blocked:
            print(
                f"[FAIL] {', '.join(blocked)} — предыдущее изменение видимости не "
                "подтверждено (uncertain). Проверьте видимость на hh.ru вручную перед повтором."
            )
            return True

    def _body(progress: ApplyProgress) -> bool:
        failed = False
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            for resume in resumes:
                attempt = (
                    None
                    if args.dry_run
                    else DurableMutationAttempt(history, progress, resume.resume_id, action)
                )
                try:
                    result = set_resume_visibility_on_hh(
                        page,
                        resume,
                        args.mode,
                        args.dry_run,
                        add_employers=add_employers,
                        remove_employers=remove_employers,
                        before_click=attempt.before_click if attempt is not None else None,
                    )
                except BaseException as exc:
                    if attempt is not None:
                        attempt.interrupt(exc)
                    raise
                if attempt is not None:
                    attempt.finish(result)
                if not result.success:
                    if result.ambiguous_candidates:
                        _print_ambiguous(result.ambiguous_query, result.ambiguous_candidates)
                    else:
                        prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
                        print(f"{prefix} {resume.id} — {result.reason}")
                    failed = True
                    continue
                if args.dry_run:
                    print(f"[DRY-RUN] Резюме {resume.id}: {result.reason}")
                else:
                    print(f"[OK] Резюме {resume.id}: {result.reason}")
        return failed

    return run_supervised_command(
        command=getattr(args, "command", "resume-visibility"),
        history=history,
        requested_limit=None,
        body=_body,
    )

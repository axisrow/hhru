"""CLI for LLM planning of the desired-work resume section (#259)."""

from __future__ import annotations

import argparse

from .copy_resume import confirm_write


class _SaveConfirmationUncertain(RuntimeError):
    """Post-click grey-zone failure marker (#465 review, round 3).

    Raised only when the SAVE click already landed and confirmation
    couldn't be verified (CLAUDE.md #207 grey-zone). A dedicated type, not a
    substring match on the exception message, is the discriminator between
    'uncertain' and 'failed' — text matching is fragile and was the exact
    class of bug found in edit_skills.py/edit_languages.py in this round.
    """


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "resume-position",
        help="Предложить и заполнить LLM значения желаемой работы в резюме",
    )
    p.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    # Ручной ввод (#326): любое из этих полей отключает LLM-планирование.
    p.add_argument(
        "--title",
        help="Готовая желаемая должность без LLM (#326)",
    )
    p.add_argument(
        "--specialization",
        action="append",
        help=(
            "Специализация (можно несколько); селектор hh.ru не подтверждён — "
            "запись упадёт fail-closed"
        ),
    )
    p.add_argument("--salary", type=int, help="Зарплата (целое число, без LLM)")
    p.add_argument("--currency", choices=("RUR", "EUR", "USD"), help="Валюта зарплаты")
    p.add_argument(
        "--employment",
        action="append",
        choices=("full_time", "part_time", "project", "internship", "volunteer"),
        help="Тип занятости (можно несколько)",
    )
    p.add_argument(
        "--work-format",
        action="append",
        choices=("office", "hybrid", "remote"),
        help="Формат работы (можно несколько)",
    )
    p.add_argument(
        "--commute",
        choices=("no_limit", "up_to_1_hour", "up_to_2_hours", "up_to_3_hours"),
        help="Время в пути",
    )
    p.add_argument(
        "--business-trips",
        choices=("true", "false"),
        help="Готовность к командировкам",
    )
    p.add_argument(
        "--mode",
        choices=("from-scratch", "fill"),
        help="Режим LLM-планирования (по умолчанию fill); не сочетается с ручными полями",
    )
    p.add_argument("--dry-run", action="store_true", help="Показать план без изменения hh.ru")
    p.add_argument("--force", action="store_true", help="Подтвердить запись без prompt")
    p.set_defaults(func=run)


def _print_plan(plan) -> None:
    print("[DRY-RUN] Предложенные значения раздела желаемой работы:")
    for key, value in vars(plan).items():
        print(f"  {key}: {value}")


def _run(args: argparse.Namespace, progress) -> bool:
    from ..ai.llm_client import LLMClient
    from ..browser import BrowserLaunchError, launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..resume_position import (
        CANCEL,
        SAVE,
        PositionValues,
        apply_position,
        build_position_prompt,
        fill_only_missing,
        open_position_form,
        parse_position_response,
    )

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    # Ручной ввод (#326): любое готовое поле отключает LLM; facts-гварды и
    # fill/from-scratch не применяются — значения даёт вызывающий, как в
    # edit-skills --skill.
    manual = any(
        value is not None
        for value in (
            getattr(args, name, None)
            for name in (
                "title",
                "specialization",
                "salary",
                "currency",
                "employment",
                "work_format",
                "commute",
                "business_trips",
            )
        )
    )
    # "fill" совпадает с неявным дефолтом ручного режима — не считаем конфликтом,
    # чтобы явная передача дефолтного значения не наказывалась (#327).
    if manual and args.mode not in (None, "fill"):
        print("[FAIL] --mode относится к LLM-планированию и не сочетается с ручными полями (#326)")
        return True
    mode = args.mode or "fill"

    # needs='ai_profile': точечная ошибка вместо «резюме не найдено в конфиге» (#319).
    try:
        resume = resolve_resume(config, args.resume, needs=() if manual else ("ai_profile",))
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True
    if not manual and config.ai is None:
        print("[FAIL] Для resume-position нужна секция ai в config.yaml")
        return True
    profile = resume.ai_profile
    try:
        llm = None if manual else LLMClient(config.ai)
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            current = open_position_form(page, resume)
            if manual:
                plan = PositionValues(
                    title=getattr(args, "title", None),
                    salary=getattr(args, "salary", None),
                    currency=getattr(args, "currency", None),
                    specializations=getattr(args, "specialization", None),
                    employment=getattr(args, "employment", None),
                    work_format=getattr(args, "work_format", None),
                    commute=getattr(args, "commute", None),
                    business_trips=(
                        None
                        if getattr(args, "business_trips", None) is None
                        else args.business_trips == "true"
                    ),
                )
            else:
                response = llm.chat(build_position_prompt(profile, current, mode))
                plan = parse_position_response(response.content)
                if current.salary is None and plan.salary is not None:
                    raise RuntimeError(
                        "LLM предложил зарплату без подтверждённого факта пользователя; "
                        "значение отклонено"
                    )
                if current.employment is None and plan.employment:
                    raise RuntimeError(
                        "LLM предложил тип занятости без подтверждённого факта пользователя; "
                        "значение отклонено"
                    )
                if current.work_format is None and plan.work_format:
                    raise RuntimeError(
                        "LLM предложил формат работы без подтверждённого факта пользователя; "
                        "значение отклонено"
                    )
                if current.commute is None and plan.commute is not None:
                    raise RuntimeError(
                        "LLM предложил время в пути без подтверждённого факта пользователя; "
                        "значение отклонено"
                    )
                if current.business_trips is None and plan.business_trips is not None:
                    raise RuntimeError(
                        "LLM предложил командировки без подтверждённого факта пользователя; "
                        "значение отклонено"
                    )
                if mode == "fill":
                    plan = fill_only_missing(current, plan)
                elif any(
                    value not in (None, "", [])
                    for value in (
                        current.title,
                        current.salary,
                        current.employment,
                        current.work_format,
                        current.commute,
                        current.business_trips,
                    )
                ):
                    raise RuntimeError("режим from-scratch требует пустого раздела")
            _print_plan(plan)
            if args.dry_run:
                page.locator(CANCEL).click()
                print("[INFO] Ничего не записано на hh.ru.")
                return False
            if not confirm_write(
                args.force, prompt=f"Записать раздел желаемой работы резюме '{resume.id}' на hh.ru?"
            ):
                page.locator(CANCEL).click()
                print("[FAIL] Нужен --force или интерактивное подтверждение. Ничего не записано.")
                return True
            progress.begin_attempt()
            apply_position(page, plan)
            if page.locator(SAVE).count() != 1:
                raise RuntimeError("кнопка сохранения формы не подтверждена")
            try:
                page.locator(SAVE).click()
                page.locator("[data-qa='resume-edit-position-form']").wait_for(
                    state="hidden", timeout=10_000
                )
            except Exception as exc:  # click already landed; result is uncertain
                raise _SaveConfirmationUncertain(
                    f"сохранение не подтверждено (uncertain) после клика: {exc}"
                ) from exc
            progress.applied_count += 1
            print(f"[OK] Раздел желаемой работы резюме '{resume.id}' обновлён.")
            return False
    except _SaveConfirmationUncertain as exc:
        # #465 review round 3: the grey-zone post-click failure (CLAUDE.md
        # #207) must be recorded as uncertain, not failed — the save click
        # already landed and may have taken effect. A dedicated exception
        # type (not a substring match on the message) is the discriminator,
        # per the code-reviewer's round-3 finding that acted/uncertain must
        # be a structural signal, never free-text matching.
        if progress.attempted_count and not progress.applied_count:
            progress.uncertain_count += 1
        print(f"[FAIL] (uncertain) {exc}")
        return True
    except BrowserLaunchError:
        # #465 review round 3: re-raise so cli.py's dedicated handler
        # (prints "[ENVIRONMENT] ..." and exits distinctly) still fires,
        # instead of the broad except Exception below swallowing it.
        raise
    except Exception as exc:
        # #465 review: applied_count is only ever incremented right above,
        # immediately before `return False` exits the `with` block. If
        # context.__exit__ itself raises during that unwind, this handler
        # would otherwise also add failed_count for the same attempt —
        # progress.applied_count is the guard against double-counting one
        # attempt as both a success and a failure.
        if progress.attempted_count and not progress.applied_count:
            progress.failed_count += 1
        print(f"[FAIL] {exc}")
        return True


def run(args: argparse.Namespace):
    """Execute one resume-edit command under the durable command-run ledger."""
    from ._common import run_single_mutation_command

    return run_single_mutation_command(command="resume_position", args=args, body=_run)

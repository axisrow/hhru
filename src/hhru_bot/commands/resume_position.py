"""CLI for LLM planning of the desired-work resume section (#259)."""

from __future__ import annotations

import argparse

from ._professional_role_guidance import print_professional_role_guidance
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
            "Точная профессия из live-каталога hh.ru; для черновика одна, "
            "для опубликованного резюме можно несколько"
        ),
    )
    p.add_argument("--salary", type=int, help="Зарплата (целое число, без LLM)")
    p.add_argument("--currency", choices=("RUR", "EUR", "USD"), help="Валюта зарплаты")
    p.add_argument(
        "--employment",
        action="append",
        choices=("full_time", "part_time", "internship", "volunteer"),
        help="Тип занятости (пока только одно значение — #526)",
    )
    p.add_argument(
        "--work-format",
        action="append",
        choices=("office", "hybrid", "remote"),
        help="Формат работы (пока только одно значение — #526)",
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
    p.add_argument(
        "--allow-auto-publish",
        action="store_true",
        help=(
            "Разрешить закрытие professional_role, после которого hh.ru может "
            "автоматически опубликовать резюме"
        ),
    )
    p.set_defaults(func=run)


def _professional_role_closes_resume(flow) -> bool:
    """Return whether this write can trigger hh.ru auto-publication.

    ``nextIncompleteScreenId=professional_role`` is the only state in which
    this command closes the last missing screen; an editor flow or another
    incomplete screen remains reversible and must not be blocked.
    """
    return flow.kind == "wizard" and flow.state.next_incomplete_screen_id == "professional_role"


def _print_plan(plan) -> None:
    print("[DRY-RUN] Предложенные значения раздела желаемой работы:")
    for key, value in vars(plan).items():
        print(f"  {key}: {value}")


def _print_classification(role, *, reason: str = "", queries: list[str] | None = None) -> None:
    print("[CLASSIFICATION] Согласование профессии live-каталога hh.ru:")
    print(f"  role_id: {role.role_id}")
    print(f"  profession: {role.label}")
    if role.category:
        print(f"  category: {role.category}")
    if queries:
        print(f"  catalog_queries: {queries}")
    if reason:
        print(f"  reason: {reason}")


def _click_save_and_wait(page) -> None:
    """Click the editor SAVE button and wait for the form to close.

    Shared by the pure-editor path and the wizard-minimum fallback (#890):
    both end up applying the plan through ``apply_position`` on the same
    ``/resume/edit/{id}/position`` form and must confirm the same way. Any
    failure here is a grey-zone post-click failure — the caller wraps it in
    ``_SaveConfirmationUncertain`` once the mutating click has landed.
    """
    from ..resume_position import SAVE

    if page.locator(SAVE).count() != 1:
        raise RuntimeError("кнопка сохранения формы не подтверждена")
    page.locator(SAVE).click()
    page.locator("[data-qa='resume-edit-position-form']").wait_for(state="hidden", timeout=10_000)


def _run(args: argparse.Namespace, progress) -> bool:
    from ..ai.llm_client import LLMClient
    from ..browser import BrowserLaunchError, launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..resume_position import (
        CANCEL,
        PositionValues,
        apply_position,
        build_position_prompt,
        fill_only_missing,
        is_position_wizard,
        open_position_form,
        parse_position_response,
        save_position_wizard_minimum,
        validate_wizard_plan,
        verify_wizard_minimum_save,
        verify_wizard_save,
    )

    config = load_config_or_exit(args.config)
    from ._common import MutationOutcome, resolve_resume

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

    try:
        resume = resolve_resume(config, args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True
    if not manual and resume.ai_profile is None:
        print(
            f"[FAIL] Для резюме '{resume.id}' не настроен ai_profile, поэтому CLI "
            "не может автоматически определить должность и профессию."
        )
        print_professional_role_guidance(resume)
        return True
    if not manual and config.ai is None:
        print("[FAIL] Для resume-position нужна секция ai в config.yaml")
        return True
    profile = resume.ai_profile
    try:
        llm = None if manual else LLMClient(config.ai)
        explicit_specialization = getattr(args, "specialization", None)
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            # Resolving an explicit role uses the read-only vacancy catalog. In
            # dry-run there is no reason to enter the resume wizard afterwards:
            # its entry card is a different UI and its first NEXT can publish a
            # draft. Keep the wizard entirely untouched in this path (#904).
            if args.dry_run and explicit_specialization:
                flow = open_position_form(page, resume, enter_wizard=False)
            else:
                flow = open_position_form(page, resume)
            current = flow.values
            wizard = flow.kind == "wizard"
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

            role = None
            classification_reason = ""
            classification_queries: list[str] = []
            if wizard:
                from ..professional_roles import resolve_explicit_role, suggest_role

                effective_title = plan.title or current.title
                if not effective_title:
                    print_professional_role_guidance(resume)
                    raise RuntimeError("для professional_role требуется непустой --title")
                if explicit_specialization:
                    if len(explicit_specialization) != 1:
                        raise RuntimeError(
                            "для professional_role требуется ровно один --specialization"
                        )
                    role = resolve_explicit_role(page, explicit_specialization[0])
                else:
                    if config.ai is None:
                        print_professional_role_guidance(resume)
                        raise RuntimeError(
                            "для подбора профессии нужна секция ai; либо передайте "
                            "точный --specialization из live-каталога"
                        )
                    if llm is None:
                        llm = LLMClient(config.ai)
                    role, classification_reason, classification_queries = suggest_role(
                        page, llm, effective_title
                    )
                plan.title = effective_title
                plan.specializations = [role.label]
                validate_wizard_plan(plan)
            # #911: должности в аккаунте уникальны — дубликат отклоняется ДО
            # клика сохранения: после клика отказ hh.ru молчит (живая проверка
            # пользователя, 2026-09-01). Проверка читает список отдельной
            # вкладкой того же контекста: визард/редактор уже открыты на текущей
            # странице, и уход на список их не должен закрывать. Карточка самого
            # резюме исключается: сохранить должность, которую оно уже носит, —
            # не дубль.
            written_title = (plan.title or "").strip()
            if written_title:
                from ..resume_titles import account_duplicate_reason

                # Вкладка не закрывается руками: команда завершается выходом
                # из launch_context, который закрывает все страницы контекста.
                duplicate_reason = account_duplicate_reason(
                    context.new_page(), written_title, exclude_resume_id=resume.resume_id
                )
                if duplicate_reason:
                    if not wizard:
                        page.locator(CANCEL).click()
                    print(f"[FAIL] {duplicate_reason}")
                    return True
            auto_publish = _professional_role_closes_resume(flow)
            if auto_publish and not args.dry_run and not getattr(args, "allow_auto_publish", False):
                print(
                    "[FAIL] professional_role — это последний незаполненный экран: "
                    "hh.ru может автоматически опубликовать резюме после сохранения. "
                    "Ничего не записано. Для явного разрешения используйте "
                    "--allow-auto-publish вместе с --force."
                )
                return True
            if auto_publish and not args.dry_run:
                print(
                    "[WARN] Следующий клик сохранения professional_role может "
                    "автоматически опубликовать резюме на hh.ru. "
                    "Разрешено флагом --allow-auto-publish."
                )
            _print_plan(plan)
            if role is not None:
                _print_classification(
                    role,
                    reason=classification_reason,
                    queries=classification_queries,
                )
            if args.dry_run:
                if not wizard:
                    page.locator(CANCEL).click()
                print("[INFO] Ничего не записано на hh.ru.")
                return False
            if (
                wizard
                and not explicit_specialization
                and not confirm_write(
                    False,
                    prompt=(
                        f"Согласовать классификацию '{plan.title}' -> "
                        f"'{role.label}' (role_id={role.role_id})?"
                    ),
                )
            ):
                print(
                    "[FAIL] Классификация не согласована. Повторите dry-run и передайте "
                    "подтверждённый --specialization. Ничего не записано."
                )
                return True
            if not confirm_write(
                args.force, prompt=f"Записать раздел желаемой работы резюме '{resume.id}' на hh.ru?"
            ):
                if not wizard:
                    page.locator(CANCEL).click()
                print("[FAIL] Нужен --force или интерактивное подтверждение. Ничего не записано.")
                return True
            if wizard:
                # Catalog resolution navigates to the read-only vacancy filter;
                # reopen and re-bind the exact draft immediately before WRITE.
                write_flow = open_position_form(page, resume)
                if (
                    write_flow.kind != "wizard"
                    or write_flow.resume_id != resume.resume_id
                    or not is_position_wizard(page, resume.resume_id)
                ):
                    raise RuntimeError("professional_role identity потерян перед WRITE")
                progress.begin_attempt()
                first_click_started = False

                def mark_first_click_started() -> None:
                    nonlocal first_click_started
                    first_click_started = True

                verified_state = None
                published_note = (
                    "[INFO] professional_role завершён; публикация требует "
                    "отдельной read-only проверки."
                )
                try:
                    # The chip wizard cannot represent role_id, and its first
                    # NEXT may close the screen immediately. Do not attempt an
                    # exact save there: record the known-wrong prerequisite
                    # category directly, then perform the mandatory fixup.
                    save_position_wizard_minimum(
                        page, resume, before_first_click=mark_first_click_started
                    )
                    verify_wizard_minimum_save(page, resume)
                    fixup_flow = open_position_form(page, resume)
                    if fixup_flow.kind != "editor" or fixup_flow.resume_id != resume.resume_id:
                        raise RuntimeError(
                            "wizard-minimum сохранён, но форма не перешла в editor-режим"
                        ) from None
                    try:
                        apply_position(page, plan, current=fixup_flow.values)
                        _click_save_and_wait(page)
                        verified_state = verify_wizard_save(
                            page,
                            resume,
                            expected_title=plan.title or "",
                            expected_role_id=role.role_id,
                            expected_role_label=role.label,
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            "осознанно неверная профессия wizard-minimum сохранена, "
                            "но обязательное исправление в editor-режиме не удалось: "
                            f"{exc}"
                        ) from exc
                    published_note = (
                        "[INFO] professional_role завершён через wizard-minimum "
                        "fallback (#890); точная специализация применена в "
                        "editor-режиме."
                    )
                except Exception as exc:
                    if not first_click_started:
                        raise
                    raise _SaveConfirmationUncertain(
                        f"сохранение professional_role не подтверждено (uncertain): {exc}"
                    ) from exc
                progress.finish(MutationOutcome(success=True))
                print(f"[OK] professional_role резюме '{resume.id}' сохранён и проверен.")
                from ..resume_state import is_published

                if verified_state is not None and is_published(verified_state):
                    print("[INFO] hh.ru подтвердил автоматическую публикацию: isSearchable=true.")
                else:
                    print(published_note)
                return False
            progress.begin_attempt()
            apply_position(page, plan, current=current)
            try:
                _click_save_and_wait(page)
            except Exception as exc:  # click already landed; result is uncertain
                raise _SaveConfirmationUncertain(
                    f"сохранение не подтверждено (uncertain) после клика: {exc}"
                ) from exc
            progress.finish(MutationOutcome(success=True))
            print(f"[OK] Раздел желаемой работы резюме '{resume.id}' обновлён.")
            return False
    except _SaveConfirmationUncertain as exc:
        # #465 review round 3: the grey-zone post-click failure (CLAUDE.md
        # #207) must be recorded as uncertain, not failed — the save click
        # already landed and may have taken effect. A dedicated exception
        # type (not a substring match on the message) is the discriminator,
        # per the code-reviewer's round-3 finding that acted/uncertain must
        # be a structural signal, never free-text matching.
        if progress.attempted_count:
            progress.finish(exc, uncertain_exceptions=(_SaveConfirmationUncertain,))
        print(f"[FAIL] (uncertain) {exc}")
        return True
    except BrowserLaunchError:
        # #465 review round 3: re-raise so cli.py's dedicated handler
        # (prints "[ENVIRONMENT] ..." and exits distinctly) still fires,
        # instead of the broad except Exception below swallowing it.
        raise
    except Exception as exc:
        # If context.__exit__ raises after the success above, ApplyProgress's
        # one-finish-per-attempt guard prevents counting the same attempt again.
        if progress.attempted_count:
            progress.finish(exc)
        print(f"[FAIL] {exc}")
        return True


def run(args: argparse.Namespace):
    """Execute one resume-edit command under the durable command-run ledger."""
    from ._common import run_single_mutation_command

    return run_single_mutation_command(command="resume_position", args=args, body=_run)

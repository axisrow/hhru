"""CLI for LLM planning of the desired-work resume section (#259)."""

from __future__ import annotations

import argparse

from .copy_resume import confirm_write


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "resume-position",
        help="Предложить и заполнить LLM значения желаемой работы в резюме",
    )
    p.add_argument("--resume", required=True, help="ID резюме из конфига")
    p.add_argument("--mode", choices=("from-scratch", "fill"), default="fill")
    p.add_argument("--dry-run", action="store_true", help="Показать план без изменения hh.ru")
    p.add_argument("--force", action="store_true", help="Подтвердить запись без prompt")
    p.set_defaults(func=run)


def _print_plan(plan) -> None:
    print("[DRY-RUN] Предложенные значения раздела желаемой работы:")
    for key, value in vars(plan).items():
        print(f"  {key}: {value}")


def run(args: argparse.Namespace) -> bool:
    from ..ai.llm_client import LLMClient
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..resume_position import (
        CANCEL,
        SAVE,
        apply_position,
        build_position_prompt,
        fill_only_missing,
        open_position_form,
        parse_position_response,
    )

    config = load_config_or_exit(args.config)
    try:
        resume = config.get_resume(args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return True
    if config.ai is None:
        print("[FAIL] Для resume-position нужна секция ai в config.yaml")
        return True
    profile = getattr(resume, "ai_profile", None)
    if profile is None:
        print("[FAIL] Для resume-position нужна секция ai_profile у резюме")
        return True
    try:
        llm = LLMClient(config.ai)
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            current = open_position_form(page, resume)
            response = llm.chat(build_position_prompt(profile, current, args.mode))
            plan = parse_position_response(response.content)
            if current.salary is None and plan.salary is not None:
                raise RuntimeError(
                    "LLM предложил зарплату без подтверждённого факта пользователя; "
                    "значение отклонено"
                )
            if args.mode == "fill":
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
            apply_position(page, plan)
            if page.locator(SAVE).count() != 1:
                raise RuntimeError("кнопка сохранения формы не подтверждена")
            page.locator(SAVE).click()
            page.locator("[data-qa='resume-edit-position-form']").wait_for(
                state="hidden", timeout=10_000
            )
            print(f"[OK] Раздел желаемой работы резюме '{resume.id}' обновлён.")
            return False
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return True

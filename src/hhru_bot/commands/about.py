"""CLI command for the LLM-assisted resume ``Обо мне`` section (#260)."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, cast

from .copy_resume import confirm_write

if TYPE_CHECKING:
    from ..config_sections.ai_profile import AIProfile


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "about",
        help="Предложить и при подтверждении сохранить текст раздела «Обо мне»",
        description=(
            "Генерирует текст раздела «Обо мне» через настроенный LLM. Сначала всегда "
            "показывает dry-run-предложение; сохранение требует --force или подтверждения."
        ),
    )
    parser.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    parser.add_argument(
        "--text",
        help=(
            "Готовый текст раздела «Обо мне» без LLM (#326): "
            "ai_profile/секция ai не требуются, текст сохраняется как есть"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать предложение без сохранения"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить сохранение без prompt")
    parser.set_defaults(func=run)


def draft_prefix(dry_run: bool) -> str:
    """Keep the dry-run marker exclusive to the no-write path."""
    return "[DRY-RUN]" if dry_run else "[INFO] Предложение"


def run(args: argparse.Namespace) -> None:
    from ..about import AboutGenerationError, generate_about, open_about_editor, save_about
    from ..ai.llm_client import LLMClient
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit

    config = load_config_or_exit(args.config)
    from ._common import resolve_resume

    # --text (#326): готовый текст в обход LLM — как edit-skills --skill,
    # ручной ввод не требует ни ai_profile, ни секции ai.
    manual = getattr(args, "text", None) is not None
    needs = () if manual else ("ai_profile",)
    try:
        resume = resolve_resume(config, args.resume, needs=needs)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
    llm = None
    ai_profile = None
    if not manual:
        if config.ai is None:
            print("[FAIL] Для команды about нужна секция ai в config.yaml")
            sys.exit(1)
        # ResumeConfig.ai_profile is typed as a neutral `object | None` placeholder
        # (see CLAUDE.md config_sections) shared across unrelated features; #17
        # (about) owns the AIProfile shape, so narrow it here at the point of use.
        ai_profile = cast("AIProfile", resume.ai_profile)
        try:
            llm = LLMClient(config.ai)
        except ImportError as exc:
            print(f"[FAIL] AI-зависимость недоступна: {exc}")
            sys.exit(1)

    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            existing = open_about_editor(page, resume)
            if manual:
                text, mode = args.text, "manual"
            else:
                draft = generate_about(llm, existing, ai_profile)
                text, mode = draft.text, draft.mode
            print(f"{draft_prefix(args.dry_run)} «Обо мне» ({mode}):\n{text}")
            if args.dry_run:
                print("[INFO] Ничего не сохранено.")
                return
            if text.strip() == existing.strip():
                print("[INFO] Новый текст не предложен; существующее содержимое не изменено.")
                return
            if not confirm_write(
                args.force,
                prompt=f"Сохранить новый текст «Обо мне» для резюме '{resume.id}' на hh.ru?",
            ):
                print("[FAIL] Нужен --force или интерактивное подтверждение. Ничего не сохранено.")
                sys.exit(1)
            save_about(page, text)
    except AboutGenerationError as exc:
        print(f"[FAIL] {resume.id} — {exc}")
        sys.exit(1)

    print(f"[OK] Раздел «Обо мне» резюме {resume.id} сохранён")

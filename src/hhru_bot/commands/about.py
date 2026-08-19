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

    # needs='ai_profile': точечная ошибка вместо «резюме не найдено в конфиге» (#319).
    try:
        resume = resolve_resume(config, args.resume, needs=("ai_profile",))
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
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
            draft = generate_about(llm, existing, ai_profile)
            print(f"{draft_prefix(args.dry_run)} «Обо мне» ({draft.mode}):\n{draft.text}")
            if args.dry_run:
                print("[INFO] Ничего не сохранено.")
                return
            if draft.text.strip() == existing.strip():
                print("[INFO] Новый текст не предложен; существующее содержимое не изменено.")
                return
            if not confirm_write(
                args.force,
                prompt=f"Сохранить новый текст «Обо мне» для резюме '{resume.id}' на hh.ru?",
            ):
                print("[FAIL] Нужен --force или интерактивное подтверждение. Ничего не сохранено.")
                sys.exit(1)
            save_about(page, draft.text)
    except AboutGenerationError as exc:
        print(f"[FAIL] {resume.id} — {exc}")
        sys.exit(1)

    print(f"[OK] Раздел «Обо мне» резюме {resume.id} сохранён")

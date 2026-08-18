"""CLI command for the LLM-assisted resume ``Обо мне`` section (#260)."""

from __future__ import annotations

import argparse
import sys

from .copy_resume import confirm_write


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "about",
        help="Предложить и при подтверждении сохранить текст раздела «Обо мне»",
        description=(
            "Генерирует текст раздела «Обо мне» через настроенный LLM. Сначала всегда "
            "показывает dry-run-предложение; сохранение требует --force или подтверждения."
        ),
    )
    parser.add_argument("--resume", required=True, help="ID резюме из конфига")
    parser.add_argument(
        "--dry-run", action="store_true", help="Показать предложение без сохранения"
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить сохранение без prompt")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..about import AboutGenerationError, generate_about, open_about_editor, save_about
    from ..ai.llm_client import LLMClient
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit

    config = load_config_or_exit(args.config)
    try:
        resume = config.get_resume(args.resume)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
    if config.ai is None or resume.ai_profile is None:
        print("[FAIL] Для команды about нужны секция ai и ai_profile у резюме")
        sys.exit(1)
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
            draft = generate_about(llm, existing, resume.ai_profile)
            print(f"[DRY-RUN] «Обо мне» ({draft.mode}):\n{draft.text}")
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

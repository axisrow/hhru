"""Команда copy-resume: создать копию резюме на hh.ru (#116, WRITE-hh-ru).

На hh.ru действие называется «Дублировать»; в референсе s3rgeym — clone-resume.
Единый контракт подтверждения WRITE-hh-ru (§1 cli-spec): боевой режим требует
--force ИЛИ интерактивного prompt в TTY; неинтерактивно без --force — exit 1.
Дневные лимиты/кулдауны throttle к copy-resume не применяются (разовая
операция, согласовано в cli-spec §3.3), но аудит в actions обязателен.

Новый resume_id НАМЕРЕННО не пишется в config.yaml — копию надо сперва
проверить руками на hh.ru; команда печатает готовый YAML-фрагмент для вставки.
"""

from __future__ import annotations

import argparse
import sys


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "copy-resume",
        help="Создать копию резюме на hh.ru (дублировать)",
        description=(
            "Создаёт копию резюме на hh.ru — то же, что «Дублировать» в меню резюме "
            "(в референсах — клонирование). WRITE-команда: боевой режим требует --force "
            "или интерактивного подтверждения; --dry-run ничего не отправляет."
        ),
    )
    p.add_argument("--resume", required=True, help="ID резюме из конфига")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, без реальных действий",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Подтвердить боевой запуск без интерактивного вопроса",
    )
    p.set_defaults(func=run)


def confirm_write(
    force: bool,
    *,
    prompt: str,
    isatty_fn=None,
    input_fn=input,
) -> bool:
    """Единый контракт подтверждения WRITE-hh-ru (§1 cli-spec, общий с будущими
    reply-employers/clear-negotiations): --force → да; интерактивный TTY →
    спросить [y/N]; неинтерактивно без --force → отказ."""
    if force:
        return True
    isatty = (isatty_fn or sys.stdin.isatty)()
    if not isatty:
        return False
    answer = input_fn(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes", "д", "да")


def format_config_snippet(new_resume_id: str) -> str:
    """YAML-фрагмент для ручной вставки в config.yaml (автозаписи нет намеренно)."""
    return (
        "[INFO] Добавьте в config.yaml:\n"
        '         - id: "<придумайте-id>"\n'
        f'           resume_url: "https://hh.ru/resume/{new_resume_id}"'
    )


def run(args: argparse.Namespace) -> None:
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..copy_resume import copy_resume_on_hh
    from ..history import History

    config = load_config_or_exit(args.config)
    try:
        resume = config.get_resume(args.resume)
    except ConfigError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)
    history = History(args.history)

    if args.dry_run:
        print(f"[DRY-RUN] Копирование резюме {resume.id} (resume_id {resume.resume_id})")
    else:
        if not confirm_write(args.force, prompt=f"Создать копию резюме '{resume.id}' на hh.ru?"):
            print(
                "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
                "Ничего не отправлено."
            )
            sys.exit(1)
        if history.count_today(resume.resume_id, "copy_resume") > 0:
            print(
                f"[INFO] Уже копировали {resume.id} сегодня — "
                "повторный запуск создаст ещё один дубль на hh.ru."
            )

    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            result = copy_resume_on_hh(page, resume, args.dry_run)
    except Exception as e:
        # Клик по «Дублировать» уже мог уйти на hh.ru (POST /applicant/resumes/clone)
        # раньше, чем упало исключение (например, goto_hh при diff-fallback исчерпал
        # ретраи) — копия на hh.ru могла создаться, а локальной записи не будет.
        # Пишем неуспех в аудит перед прокидыванием исключения дальше, а не молчим.
        history.record_action(
            resume.resume_id,
            resume.resume_id,
            "copy_resume",
            "failed",
            f"исключение в браузерном шаге: {e}",
        )
        raise

    # Fail-closed (#116): success без нового id или с id, совпавшим с исходным,
    # успехом не считается — копия не подтверждена.
    if result.success and not args.dry_run:
        if not result.new_resume_id or result.new_resume_id == resume.resume_id:
            result.success = False
            result.reason = "новый resume_id не подтверждён (совпал с исходным или пуст)"

    status = "dry_run" if args.dry_run else ("success" if result.success else "failed")
    reason = f"new_resume_id={result.new_resume_id}" if status == "success" else result.reason
    # Для action='copy_resume' нет vacancy_id (операция над резюме, не отклик) —
    # как у bump, заполняем sentinel'ом resume_id; UNIQUE-индекс actions существует
    # только WHERE action='apply', коллизий нет.
    history.record_action(resume.resume_id, resume.resume_id, "copy_resume", status, reason)

    if args.dry_run:
        if not result.success:
            print(f"[FAIL] {resume.id} — {result.reason}")
            sys.exit(1)
        print("[INFO] Ничего не отправлено.")
    elif result.success:
        print(f"[OK] Резюме {resume.id} скопировано. Новый resume_id: {result.new_resume_id}")
        print(format_config_snippet(result.new_resume_id))
    else:
        print(f"[FAIL] {resume.id} — {result.reason}")
        sys.exit(1)

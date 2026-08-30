"""CLI для генерации содержимого резюме под кластер вакансий (#753) и его
применения на hh.ru (#769, PR-2 эпика #750).

Без ``--apply`` команда остаётся read-only (PR-1 #753): только генерация и
печать плана, браузер не открывается вовсе. С ``--apply`` открывается
браузер и применяются title/about/skills через ``adaptive_resume_apply.py``
(work_experience/projects сознательно вне скоупа — см. его docstring).
"""

from __future__ import annotations

import argparse
import sys

from ..resume_clusters import CLUSTERS, cluster_by_key


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "adaptive-resume",
        help="Сгенерировать содержимое резюме под кластер вакансий (LLM) и применить его на hh.ru",
        description=(
            "Генерирует заголовок, «Обо мне», порядок навыков и отбор мест работы/"
            "проектов под один из четырёх кластеров вакансий (#752). Без --apply "
            "только показывает предложение (браузер не открывается). С --apply "
            "применяет title/about/skills на hh.ru; боевая запись требует --force "
            "или интерактивного подтверждения. work_experience/projects этой "
            "командой на hh.ru не пишутся (#769: вне скоупа первой версии)."
        ),
    )
    parser.add_argument(
        "--resume",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru (#319)",
    )
    parser.add_argument(
        "--cluster",
        required=True,
        choices=[c.key for c in CLUSTERS],
        help="Кластер вакансий, под который адаптируется резюме",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Открыть браузер и применить title/about/skills на hh.ru (иначе только печать плана)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="С --apply: открыть формы и показать план по каждому шагу, ничего не сохраняя. "
        "Без --apply — no-op (команда и так не пишет на hh.ru, флаг оставлен для "
        "единообразия и совместимости)",
    )
    parser.add_argument(
        "--force", action="store_true", help="С --apply: подтвердить запись без TTY prompt"
    )
    parser.set_defaults(func=run)


def _print_content(resume_id: str, cluster_title: str, content) -> None:
    print(
        f"[DRY-RUN] Адаптивное резюме '{resume_id}' под кластер «{cluster_title}» "
        f"(источник: {content.source}):"
    )
    print(f"  Заголовок: {content.title}")
    print(f"  Обо мне: {content.about}")
    if content.skills:
        print(f"  Навыки (в порядке приоритета): {', '.join(content.skills)}")
    if content.work_experience:
        print("  Опыт работы:")
        for line in content.work_experience:
            print(f"    - {line}")
    if content.projects:
        print("  Проекты:")
        for line in content.projects:
            print(f"    - {line}")
    if content.hidden_note:
        print(f"  [INFO] {content.hidden_note}")
    print(
        "[INFO] work_experience/projects этой командой на hh.ru не сохраняются (#769). "
        "Передайте --apply, чтобы применить title/about/skills."
    )


def _generate(args: argparse.Namespace):
    from ..adaptive_resume import generate_adaptive_resume
    from ..ai.llm_client import LLMClient
    from ..config import ConfigError, load_config_or_exit
    from ._common import resolve_resume

    config = load_config_or_exit(args.config)
    try:
        resume = resolve_resume(config, args.resume, needs=("candidate_facts",))
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)

    cluster = cluster_by_key(args.cluster)

    llm = None
    if config.ai is not None:
        try:
            llm = LLMClient(config.ai)
        except ImportError as exc:
            print(f"[FAIL] AI-зависимость недоступна: {exc}")
            sys.exit(1)

    # Без секции ai (llm is None) generate_adaptive_resume отдаёт безопасный
    # детерминированный fallback, а не отказ команды (fail-closed).
    content = generate_adaptive_resume(llm, resume.candidate_facts, cluster)
    return config, resume, cluster, content


def _run_apply(args: argparse.Namespace, progress) -> bool:
    from ..adaptive_resume_apply import apply_adaptive_resume
    from ..browser import BrowserLaunchError, launch_context
    from ..history import History
    from .copy_resume import confirm_write

    config, resume, cluster, content = _generate(args)
    _print_content(resume.id, cluster.title, content)

    if not args.dry_run and not confirm_write(
        args.force,
        prompt=f"Применить title/about/skills резюме '{resume.id}' на hh.ru?",
    ):
        print("[FAIL] Требуется --force или интерактивное подтверждение. Ничего не сохранено.")
        return True

    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            if not args.dry_run:
                progress.begin_attempt()
            results = apply_adaptive_resume(page, resume, content, dry_run=args.dry_run)
    except BrowserLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001 - browser/auth errors are a failed command, not a traceback
        if progress.attempted_count:
            progress.finish(exc)
        print(f"[FAIL] {resume.id} — {exc}")
        return True

    if not args.dry_run:
        status = progress.finish(results)
        assert status is not None
        History(args.history).record_action(
            resume.resume_id,
            resume.resume_id,
            "adaptive_resume_apply",
            status,
            "; ".join(f"{r.step}: {r.reason}" for r in results),
            run_id=progress.run_id,
        )

    failed = False
    for item in results:
        if item.skipped:
            prefix = "[skip]"
        elif item.uncertain:
            prefix = "[FAIL] (uncertain)"
            failed = True
        elif item.success:
            prefix = "[DRY-RUN]" if args.dry_run else "[OK]"
        else:
            prefix = "[FAIL]"
            failed = True
        print(f"{prefix} {item.step}: {item.reason}")
    if args.dry_run:
        print("[INFO] Ничего не сохранено на hh.ru.")
    return failed


def run(args: argparse.Namespace):
    if not getattr(args, "apply", False):
        _, resume, cluster, content = _generate(args)
        _print_content(resume.id, cluster.title, content)
        return None

    from ._common import run_single_mutation_command

    return run_single_mutation_command(command="adaptive_resume_apply", args=args, body=_run_apply)

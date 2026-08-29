"""CLI для генерации содержимого резюме под кластер вакансий (#753).

PR-1 среза эпика #750: только генерация + ``--dry-run``, без записи на hh.ru
(применение существующими ``edit_*_on_hh`` — отдельный follow-up PR-2, см.
тело issue #753 "Риск превышения лимита 1000 строк"). Команда read-only по
своей природе: не открывает браузер вообще, работает только с локальным
конфигом и LLM.
"""

from __future__ import annotations

import argparse
import sys

from ..resume_clusters import CLUSTERS, cluster_by_key


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "adaptive-resume",
        help="Сгенерировать содержимое резюме под кластер вакансий (LLM, только dry-run)",
        description=(
            "Генерирует заголовок, «Обо мне», порядок навыков и отбор мест работы/"
            "проектов под один из четырёх кластеров вакансий (#752). Всегда "
            "показывает результат как предложение; запись на hh.ru эта команда "
            "не делает (см. #753 PR-1/PR-2)."
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
        "--dry-run",
        action="store_true",
        help="Явно показать план без сохранения (поведение по умолчанию — эта команда "
        "не пишет на hh.ru в принципе; флаг оставлен для единообразия с другими "
        "командами и совместимости с будущим PR-2)",
    )
    parser.set_defaults(func=run)


def _print_content(resume_id: str, content) -> None:
    print(
        f"[DRY-RUN] Адаптивное резюме '{resume_id}' под кластер «{content.cluster_key}» "
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
    print("[INFO] Ничего не сохранено на hh.ru — эта команда только генерирует предложение.")


def run(args: argparse.Namespace) -> None:
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

    _print_content(resume.id, content)

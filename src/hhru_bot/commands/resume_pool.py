"""Команда resume-pool: материализовать пул резюме под кластеры (#754, эпик #750).

Копирует одно заполненное базовое резюме (``--source``) по разу на каждый
недостающий кластер вакансий (``resume_clusters.CLUSTERS``, #752/#753) и
связывает каждую копию со своим кластером в config.yaml (``--write-config``).
Материализация идёт через уже существующий ``copy_resume_on_hh`` (тот же клик
"Дублировать", что у ``copy-resume``), НЕ через ``create-resume`` — тот создаёт
пустой черновик без опыта работы, непригодный для дальнейшей адаптации под
кластер (issue #769). См. корректировку к телу issue #754 (комментарий в
issue): исходный текст ссылался на ``create_resume.py`` как на равноценную
альтернативу, это неверно.

Durable-гарантии переиспользуются как есть, не обходятся ради batch-режима
(issue п.7): ``has_unresolved_uncertain(source.resume_id, "copy_resume")``
ключуется по resume_id ИСХОДНОГО резюме — тот же ключ для всех копий одного
источника. Поэтому batch строго последователен и останавливается на первом
``uncertain`` или ``failed`` — один неподтверждённый результат блокирует весь
оставшийся прогон до ручной проверки на hh.ru, как и у обычного copy-resume.

Лимит резюме hh.ru (~20) — косвенный: если ``copy_resume_on_hh`` вернёт
"кнопка Дублировать не найдена" (``duplicate_action_missing:``), это обычный
pre-click ``[FAIL]`` без угадывания счётчика (issue п.5) — reason
пробрасывается как есть.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command
from .copy_resume import (
    _set_copy_title,
    confirm_write,
    format_config_snippet,
    reject_bare_source,
    write_resume_config,
)


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "resume-pool",
        help="Материализовать пул резюме под кластеры вакансий (копии --source)",
        description=(
            "Копирует базовое резюме --source по разу на каждый недостающий "
            "кластер вакансий (resume_clusters.py) и помечает каждую копию "
            "своим кластером в config.yaml. WRITE-команда: боевой режим "
            "требует --force или интерактивного подтверждения; --dry-run "
            "показывает полный план без обращения к hh.ru."
        ),
    )
    p.add_argument(
        "--source",
        required=True,
        help="Slug из конфига или реальный resume_id HH.ru базового резюме (#319)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать план создания пула без реальных действий",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Подтвердить боевой запуск без интерактивного вопроса",
    )
    p.add_argument(
        "--write-config",
        action="store_true",
        help="Добавить каждую успешно созданную копию в config.yaml с её кластером",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Предел числа создаваемых за прогон копий (по умолчанию — все "
            "недостающие кластеры; троттлинга у copy-resume нет вовсе, поэтому "
            "предел обязателен для контролируемого batch-запуска)"
        ),
    )
    p.set_defaults(func=run)


def _print_plan(source, plan) -> None:
    total = len(plan.covered) + plan.missing_total
    print(
        f"[DRY-RUN] Пул резюме от '{source.id}' (resume_id {source.resume_id}): "
        f"требуется создать {plan.missing_total} из {total} кластеров"
    )
    for cluster in plan.covered:
        print(f"  [OK] уже покрыт: {cluster.key} ({cluster.title})")
    for i, item in enumerate(plan.items, start=1):
        print(
            f"  {i}. {item.cluster.key:<14} -> slug '{item.slug}' "
            f"title-заготовка: {item.cluster.title}"
        )
    if len(plan.items) < plan.missing_total:
        skipped = plan.missing_total - len(plan.items)
        print(f"[INFO] --limit обрезал план: {skipped} кластер(ов) останутся не покрыты")
    print("[INFO] Ничего не отправлено.")


def run(args: argparse.Namespace):
    from ..browser import launch_context
    from ..config import ConfigError, load_config_or_exit
    from ..copy_resume import copy_resume_on_hh
    from ..history import History
    from ..resume_pool import build_pool_plan
    from ._common import resolve_resume

    config = load_config_or_exit(args.config)
    try:
        source = resolve_resume(config, args.source)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
    reject_bare_source(source)

    write_config = bool(getattr(args, "write_config", False))
    plan = build_pool_plan(config.resumes, source, limit=args.limit)

    if not plan.items:
        if plan.missing_total == 0:
            print(f"[INFO] Все кластеры уже покрыты резюме из config.yaml (источник: {source.id})")
        else:
            print("[INFO] --limit не оставил ни одной копии для создания (0)")
        return

    if args.dry_run:
        _print_plan(source, plan)
        return

    if not confirm_write(
        args.force,
        prompt=(
            f"Создать {len(plan.items)} копий резюме '{source.id}' на hh.ru "
            "под недостающие кластеры?"
        ),
    ):
        print(
            "[FAIL] Боевой режим требует --force или интерактивного подтверждения. "
            "Ничего не отправлено."
        )
        sys.exit(1)

    history = History(args.history)
    if history.has_unresolved_uncertain(source.resume_id, "copy_resume"):
        print(
            f"[FAIL] {source.id} — предыдущее копирование не подтверждено (uncertain). "
            "Проверьте список резюме на hh.ru вручную перед повтором."
        )
        sys.exit(1)

    def _body(progress: ApplyProgress) -> bool:
        any_failed = False
        for i, item in enumerate(plan.items):
            # Тот же guard заново перед каждой копией: попытка N могла
            # оставить uncertain-маркер под тем же resume_id источника —
            # уважаем существующий fail-closed инвариант copy-resume, не
            # обходим его ради batch-режима (issue п.7).
            if i > 0 and history.has_unresolved_uncertain(source.resume_id, "copy_resume"):
                print(
                    f"[FAIL] пул остановлен перед кластером {item.cluster.key}: "
                    f"предыдущее копирование '{source.id}' не подтверждено (uncertain). "
                    "Проверьте hh.ru вручную перед повтором."
                )
                any_failed = True
                break

            attempt = DurableMutationAttempt(history, progress, source.resume_id, "copy_resume")
            try:
                with launch_context(
                    config.storage_state_file,
                    headless=args.headless,
                    user_agent=config.user_agent,
                ) as context:
                    page = context.new_page()
                    result = copy_resume_on_hh(
                        page, source, dry_run=False, before_click=attempt.before_click
                    )
                    if result.success and (
                        not result.new_resume_id or result.new_resume_id == source.resume_id
                    ):
                        # Тот же fail-closed контракт, что и у copy-resume
                        # (#116): совпавший/пустой resume_id — не подтверждённая
                        # копия.
                        result.success = False
                        result.reason = (
                            "новый resume_id не подтверждён (совпал с исходным или пуст)"
                        )
                    if result.success:
                        try:
                            _set_copy_title(page, result.new_resume_id, item.cluster.title)
                        except Exception as exc:
                            # Копия уже создана и необратима — тот же uncertain
                            # контракт, что у copy-resume --title (#569).
                            result.success = False
                            result.uncertain = True
                            result.reason = (
                                f"Копия создана ({result.new_resume_id}), но title не "
                                f"установлен: {exc} (uncertain; проверьте резюме на "
                                "hh.ru вручную)"
                            )
            except BaseException as exc:
                attempt.interrupt(exc)
                raise
            if result.success:
                result.reason = f"new_resume_id={result.new_resume_id}"
            attempt.finish(result)

            if not result.success:
                prefix = "[FAIL] (uncertain)" if result.uncertain else "[FAIL]"
                print(f"{prefix} кластер {item.cluster.key} — {result.reason}")
                any_failed = True
                break  # fail-closed: не продолжаем batch после отказа/uncertain

            print(f"[OK] кластер {item.cluster.key} -> resume_id {result.new_resume_id}")
            if write_config:
                try:
                    write_resume_config(
                        args.config,
                        replace(source, cluster=item.cluster.key),
                        item.slug,
                        result.new_resume_id,
                    )
                except Exception as exc:  # noqa: BLE001 - копия уже необратима на hh.ru
                    print(f"[FAIL] Копия создана, но config.yaml не обновлён: {exc}")
                    print(format_config_snippet(result.new_resume_id))
                    print(f'[INFO] Добавьте вручную: cluster: "{item.cluster.key}"')
                    any_failed = True
                    break
                print(
                    f"[OK] Резюме '{item.slug}' добавлено в config.yaml "
                    f"(cluster={item.cluster.key})"
                )
            else:
                print(format_config_snippet(result.new_resume_id))
                print(f'[INFO] Добавьте вручную: cluster: "{item.cluster.key}"')
        return any_failed

    return run_supervised_command(
        command=getattr(args, "command", "resume-pool"),
        history=history,
        requested_limit=len(plan.items),
        body=_body,
    )

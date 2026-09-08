"""Команда import-resume: создать резюме в другом аккаунте из экспорта (#1023).

WRITE-команда с гейтами: по умолчанию только ``--dry-run`` (план без кликов),
боевой запуск требует ``--force``. Мутации идут уже подтверждёнными путями
существующих команд: каркас — визард создания (#936: seam ``before_click``
на мутирующем клике визарда), позиция — строго editor
``/resume/edit/{id}/position`` (НЕ hh.ru-копия), секции — ``edit_*_on_hh``,
фото — ``upload_photo_on_hh`` с проверкой лимита галереи 8 ДО первой загрузки
(переполнение — честный отказ с планом, а не частичная тишина).

Durable-гейт ЧЕСТНО: seam ``before_click`` реально передан только туда, где
вызываемая функция его принимает — ``create_resume_on_hh`` и
``upload_photo_on_hh`` (uncertain-ledger защищает именно эти две мутации).
Секции позиция/о себе/опыт/образование/навыки/языки ведут себя как их
одиночные команды (``edit_position``/``about``/... ): durable-маркера у них
нет, сбой посреди клика классифицируется по вердиктам секций, но
``has_unresolved_uncertain`` их повтор не блокирует.

Никаких удалений — ни в исходном, ни в целевом аккаунте. Финальный отчёт —
сверка экспорт↔импорт (повторное чтение созданного резюме тем же read-путём,
что и экспорт), расхождения списком; роль-плейсхолдер «Другое» не маскируется.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ..import_resume import GALLERY_LIMIT, IMPORT_SKILL_LEVEL, ImportPlanError


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "import-resume",
        help="Создать резюме из экспорта export-resume в текущем аккаунте (write)",
        description=(
            "Читает JSON-файл команды export-resume и в ТЕКУЩЕМ аккаунте "
            "(--account) создаёт резюме: каркас через визард создания, роль — "
            "по title экспорта, затем секции — позиция (editor-режим), опыт, "
            "образование, навыки, языки, о себе, фото. Секция, которую "
            "невозможно перенести без выдумывания значения, честно пропускается. "
            "Финальный отчёт сверяет экспорт и импорт по секциям. "
            "WRITE-команда: по умолчанию только dry-run; боевой запуск требует "
            "--force. Никаких удалений."
        ),
    )
    parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="Путь к JSON-файлу экспорта (schema export-resume/v1)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data") / "imports",
        help="Каталог для контрольного re-экспорта созданного резюме (по умолчанию data/imports)",
    )
    parser.add_argument(
        "--no-photos",
        action="store_true",
        help="Не переносить фото",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать план без создания резюме (режим по умолчанию)",
    )
    parser.add_argument("--force", action="store_true", help="Подтвердить боевой импорт")
    parser.set_defaults(func=run)


def _print_plan(
    payload: dict,
    position_plan,  # noqa: ANN001 - PositionValues, импорт в рантайме
    experience_plan,  # noqa: ANN001
    education_plan,  # noqa: ANN001
    skills: list,
    languages: list,
    photos: list[Path],
    unavailable: list[str],
) -> None:
    from ..import_resume import parse_salary_text

    src = payload.get("position") or {}
    print(f"[INFO] Источник: resume_id {payload.get('resume_id')} ({payload.get('slug')})")
    print(f"[INFO] Создание: create-resume area/title = «{src.get('title')}»")
    salary, currency = parse_salary_text(src.get("salary_text"))
    if salary:
        print(f"[INFO] Позиция: salary={salary} currency={currency or 'не распознана'}")
    if position_plan.employment or position_plan.work_format:
        print(
            f"[INFO] Позиция: занятость={position_plan.employment} "
            f"формат={position_plan.work_format} "
            f"командировки={position_plan.business_trips}"
        )
    print(f"[INFO] Опыт: {len(experience_plan.entries)} записей")
    print(f"[INFO] Образование: {len(education_plan.primary)} записей")
    print(f"[INFO] Навыки: {len(skills)} (уровень {IMPORT_SKILL_LEVEL} — конвенция импорта)")
    print(f"[INFO] Языки: {len(languages)}")
    print(f"[INFO] Фото: {len(photos)} файлов (галерея аккаунта, лимит {GALLERY_LIMIT})")
    print(
        "[WARN] контакты: форма контактов hh.ru не подтверждена селекторами — секция не переносится"
    )
    for note in unavailable:
        print(f"[WARN] не переносится: {note}")


def run(args: argparse.Namespace):
    from ..config import load_config_or_exit
    from ..history import History
    from ..import_resume import (
        export_photos_on_disk,
        load_export,
        plan_education,
        plan_experience,
        plan_languages,
        plan_position,
        plan_skills,
    )

    config = load_config_or_exit(args.config)
    history = History(args.history)
    dry_run = args.dry_run or not args.force
    try:
        payload = load_export(args.file)
        position_plan, unavailable = plan_position(payload)
        experience_plan, notes = plan_experience(payload)
        unavailable += notes
        education_plan, notes = plan_education(payload)
        unavailable += notes
        skills, notes = plan_skills(payload)
        unavailable += notes
        languages, notes = plan_languages(payload)
        unavailable += notes
        photos, notes = export_photos_on_disk(payload)
        unavailable += notes
    except ImportPlanError as exc:
        print(f"[FAIL] {exc}")
        return True
    if not position_plan.title:
        print("[FAIL] В экспорте нет позиции (title) — импорт невозможен")
        return True

    from .copy_resume import confirm_write

    if not dry_run and not confirm_write(
        args.force,
        prompt=(
            f"Создать резюме «{position_plan.title}» из экспорта ({args.file}) в текущем аккаунте?"
        ),
    ):
        print("[FAIL] Боевой режим требует --force или интерактивного подтверждения.")
        return True
    if not dry_run and history.has_unresolved_uncertain("account", "create_resume"):
        print(
            "[FAIL] предыдущее создание резюме не подтверждено (uncertain). "
            "Проверьте список резюме на hh.ru вручную перед повтором."
        )
        return True
    # --no-photos действует в ОБЕИХ ветках (dry-run и боевой): флаг очищает
    # список до ветвления, иначе --force --no-photos молча загрузил бы фото
    # (#1028 review).
    if args.no_photos and photos:
        print("[WARN] --no-photos: фото из экспорта пропущены")
        photos = []

    if dry_run:
        print("[DRY-RUN] План импорта (боевой запуск требует --force):")
        _print_plan(
            payload,
            position_plan,
            experience_plan,
            education_plan,
            skills,
            languages,
            photos,
            unavailable,
        )
        return False
    return _run_live(
        args,
        history,
        config,
        payload,
        position_plan,
        experience_plan,
        education_plan,
        skills,
        languages,
        photos,
        unavailable,
    )


def _run_live(
    args,
    history,
    config,
    payload,
    position_plan,
    experience_plan,
    education_plan,
    skills,
    languages,
    photos,
    unavailable,
):
    """Боевой импорт — делегируется сервису (``commands/import_service.py``, #1049)."""
    from ._common import run_supervised_command
    from .import_service import ImportRunParams, run_import

    params = ImportRunParams(
        payload=payload,
        position_plan=position_plan,
        experience_plan=experience_plan,
        education_plan=education_plan,
        skills=skills,
        languages=languages,
        photos=photos,
        unavailable=unavailable,
        no_photos=args.no_photos,
        storage_state_file=config.storage_state_file,
        headless=args.headless,
        user_agent=config.user_agent,
        output=args.output,
    )
    return run_supervised_command(
        command=getattr(args, "command", "import-resume"),
        history=history,
        requested_limit=None,
        body=lambda progress: run_import(progress, history, params),
    )

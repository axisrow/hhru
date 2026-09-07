"""Команда import-resume: создать резюме в другом аккаунте из экспорта (#1023).

WRITE-команда с гейтами: по умолчанию только ``--dry-run`` (план без кликов),
боевой запуск требует ``--force``. Мутации идут уже подтверждёнными путями
существующих команд: каркас — визард создания (#936: seam ``before_click``
на первом NEXT), позиция — строго editor ``/resume/edit/{id}/position``
(НЕ hh.ru-копия), секции — ``edit_*_on_hh``, фото — ``upload_photo_on_hh``
с проверкой лимита галереи 8 ДО первой загрузки (переполнение — честный отказ
с планом, а не частичная тишина). Каждая мутация обёрнута в
``DurableMutationAttempt``; ``uncertain`` блокирует повтор через
``has_unresolved_uncertain``.

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


def _report(name: str, ok: bool, message: str, *, uncertain: bool, problems: list[str]) -> None:
    if ok:
        print(f"[OK] {name}: {message}")
        return
    prefix = "[WARN] (uncertain)" if uncertain else "[FAIL]"
    print(f"{prefix} {name}: {message}")
    problems.append(f"{name}: {message}")


def _read_resume_titles(page, new_id: str, title: str):  # noqa: ANN001 - Page
    """Полный map resume_id→title целевого аккаунта (read-only), или None.

    ``list_resume_cards`` читает /applicant/resumes; полный список, включая
    свежесозданный черновик, — RESUMES_FULL_LIST_URL. Неопределённое состояние
    списка (анти-бот/дрейф) — None: вызывающий код отказывается от опыта
    вместо риска over-binding #782.
    """
    from ..copy_resume import RESUMES_FULL_LIST_URL, ResumeListIndeterminate, list_resume_cards

    try:
        cards = list_resume_cards(page, navigate=True, url=RESUMES_FULL_LIST_URL)
    except ResumeListIndeterminate:
        return None
    titles = {card.resume_id: card.title for card in cards}
    titles[new_id] = title
    return titles


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
    """Боевой импорт; каждая секция — отдельная durable-попытка и вердикт."""
    from ._common import ApplyProgress, DurableMutationAttempt, run_supervised_command

    problems: list[str] = []
    discrepancies: list[str] = []

    def _body(progress: ApplyProgress) -> bool:
        nonlocal discrepancies
        from ..about import AboutGenerationError, open_about_editor, save_about
        from ..apply.antibot import AntiBotChallengeDetected
        from ..browser import launch_context
        from ..catalog_preflight import preflight_area
        from ..config import bare_resume
        from ..create_resume import apply_draft_readback, create_resume_on_hh
        from ..experience import edit_experience_on_hh
        from ..export_resume import export_resume_on_hh
        from ..import_resume import diff_export
        from ..languages import edit_languages_on_hh
        from ..resume_education import edit_education_on_hh
        from ..resume_photo import select_photo_on_hh, upload_photo_on_hh, validate_photo
        from ..resume_position import apply_position, open_position_form
        from ..skills import edit_skills_on_hh
        from .copy_resume import format_config_snippet
        from .resume_position import _click_save_and_wait

        def attempt(resume_id: str, action: str) -> DurableMutationAttempt:
            return DurableMutationAttempt(history, progress, resume_id, action)

        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            try:
                # Read-only сверка area с live-каталогом ДО любого клика (#950).
                outcome = preflight_area(page, position_plan.title, allow_unresolved_area=False)
                if not outcome.ok:
                    print(f"[FAIL] создание: {outcome.message}")
                    return True
                if outcome.message:
                    print(f"[WARN] {outcome.message}")
                create_attempt = attempt("account", "create_resume")
                try:
                    result = create_resume_on_hh(
                        page,
                        area=position_plan.title,
                        title=position_plan.title,
                        dry_run=False,
                        before_click=create_attempt.before_click,
                    )
                except BaseException as exc:
                    create_attempt.interrupt(exc)
                    raise
                create_attempt.finish(result)
                if not result.success:
                    prefix = "[WARN] (uncertain)" if result.uncertain else "[FAIL]"
                    print(f"{prefix} создание: {result.reason}")
                    return True
                print(f"[OK] создание: {result.reason} Новый resume_id: {result.new_resume_id}")
                if result.placeholder_role:
                    note = (
                        "роль установлена плейсхолдером «Другое» — точная роль из "
                        "экспорта НЕ восстановлена"
                    )
                    print(f"[WARN] роль: {note}")
                    discrepancies.append(f"роль: {note}")
                new_id = result.new_resume_id
                resume = bare_resume(new_id)
                print(format_config_snippet(new_id))
                result = apply_draft_readback(page, result)
                print(f"[INFO] readback: {result.reason}")

                # Позиция — строго editor-режим (#881): НЕ hh.ru-копия.
                pos_attempt = attempt(new_id, "edit_position")
                try:
                    flow = open_position_form(page, resume, enter_wizard=False)
                    if flow.kind != "editor":
                        raise RuntimeError(
                            "форма позиция открылась не в editor-режиме — "
                            "заполнение через hh.ru-копию запрещено"
                        )
                    apply_position(page, position_plan, current=flow.values)
                    _click_save_and_wait(page)
                except BaseException as exc:
                    pos_attempt.interrupt(exc)
                    raise
                pos_attempt.finish(True)
                _report(
                    "позиция", True, "сохранена в editor-режиме", uncertain=False, problems=problems
                )

                # О себе
                about_text = payload.get("about")
                if not about_text:
                    print("[WARN] о себе: в экспорте пусто — пропущено")
                else:
                    about_attempt = attempt(new_id, "edit_about")
                    try:
                        open_about_editor(page, resume)
                        save_about(page, about_text)
                    except AboutGenerationError as exc:
                        about_attempt.finish(False)
                        _report("о себе", False, str(exc), uncertain=False, problems=problems)
                    except BaseException as exc:
                        about_attempt.interrupt(exc)
                        raise
                    else:
                        about_attempt.finish(True)
                        _report("о себе", True, "сохранено", uncertain=False, problems=problems)

                # Опыт (append-only: у нового резюме записей нет).
                # resume_titles — ПОЛНЫЙ map резюме целевого аккаунта (#782):
                # второй+ запись идёт через shared-profile панель «Резюме с
                # этим местом работы», где все резюме стартуют pre-checked;
                # неполный map дал бы other_titles=[] и молчаливую привязку
                # импорта ко ВСЕМ резюме аккаунта (#1028 review).
                if experience_plan.entries:
                    exp_attempt = attempt(new_id, "edit_experience")
                    try:
                        resume_titles = _read_resume_titles(page, new_id, position_plan.title)
                        if resume_titles is None:
                            raise RuntimeError(
                                "список резюме целевого аккаунта не прочитан — "
                                "опыт не переносится (риск over-binding #782)"
                            )
                        rows = edit_experience_on_hh(
                            page,
                            new_id,
                            experience_plan,
                            dry_run=False,
                            resume_titles=resume_titles,
                            append_only=True,
                        )
                    except BaseException as exc:
                        exp_attempt.interrupt(exc)
                        raise
                    exp_attempt.finish(rows)
                    uncertain_rows = any(r.uncertain for r in rows)
                    bad = [r.reason for r in rows if not r.success]
                    _report(
                        "опыт",
                        not bad,
                        "; ".join(bad[:3]) or f"{len(rows)} записей",
                        uncertain=uncertain_rows,
                        problems=problems,
                    )
                else:
                    print("[WARN] опыт: переносимых записей нет — пропущено")

                # Образование (только primary: экспорт уровня не различает)
                if education_plan.primary:
                    edu_attempt = attempt(new_id, "edit_education")
                    try:
                        edu_rows = edit_education_on_hh(
                            page,
                            resume.resume_url,
                            education_plan,
                            section="primary",
                            dry_run=False,
                        )
                    except BaseException as exc:
                        edu_attempt.interrupt(exc)
                        raise
                    edu_attempt.finish(edu_rows)
                    bad = [r.reason for r in edu_rows if not r.success]
                    _report(
                        "образование",
                        not bad,
                        "; ".join(bad[:3]) or f"{len(education_plan.primary)} записей",
                        uncertain=any(r.uncertain for r in edu_rows),
                        problems=problems,
                    )
                else:
                    print("[WARN] образование: переносимых записей нет — пропущено")

                # Навыки (уровень — конвенция IMPORT_SKILL_LEVEL, печатается)
                if skills:
                    sk_attempt = attempt(new_id, "edit_key_skills")
                    try:
                        sk = edit_skills_on_hh(
                            page, resume, tuple(skills), dry_run=False, mode="append"
                        )
                    except BaseException as exc:
                        sk_attempt.interrupt(exc)
                        raise
                    sk_attempt.finish(sk)
                    _report(
                        "навыки",
                        sk.success,
                        sk.reason
                        or f"{len(sk.added or skills)} добавлено (уровень {IMPORT_SKILL_LEVEL})",
                        uncertain=False,
                        problems=problems,
                    )
                else:
                    print("[WARN] навыки: переносимых нет — пропущено")

                # Языки (аккаунт-уровень на hh.ru)
                if languages:
                    lg_attempt = attempt(new_id, "edit_languages")
                    try:
                        lg = edit_languages_on_hh(
                            page, resume, tuple(languages), dry_run=False, mode="append"
                        )
                    except BaseException as exc:
                        lg_attempt.interrupt(exc)
                        raise
                    lg_attempt.finish(lg)
                    _report(
                        "языки",
                        lg.success,
                        lg.reason or f"{len(languages)} добавлено",
                        uncertain=lg.acted and not lg.success,
                        problems=problems,
                    )
                else:
                    print("[WARN] языки: переносимых нет — пропущено")

                # Фото: лимит галереи проверяется ДО первой загрузки
                if photos:
                    inventory = select_photo_on_hh(page, resume, None, True)
                    existing = len(inventory.photos)
                    free = GALLERY_LIMIT - existing
                    if inventory.reason and not inventory.photos:
                        print(
                            "[FAIL] фото: инвентарь галереи не прочитан "
                            f"({inventory.reason}) — загрузка отменена целиком"
                        )
                        problems.append("фото: инвентарь галереи не прочитан")
                    elif free <= 0:
                        print(
                            f"[FAIL] фото: галерея аккаунта полна "
                            f"({existing}/{GALLERY_LIMIT}) — ни одно фото не перенесено. "
                            "План: освободите слоты в галерее вручную и перенесите фото "
                            "отдельным запуском upload-photo."
                        )
                        problems.append("фото: галерея переполнена, отказ с планом")
                    else:
                        fitting, overflow = photos[:free], photos[free:]
                        if overflow:
                            print(
                                f"[WARN] фото: свободных слотов {free}, файлов {len(photos)} — "
                                f"{len(overflow)} не поместятся "
                                f"({', '.join(p.name for p in overflow)}). Освободите слоты и "
                                "перенесите их отдельным запуском upload-photo."
                            )
                        for path in fitting:
                            up_attempt = attempt(new_id, "upload_photo")
                            try:
                                photo_file = validate_photo(path)
                                up = upload_photo_on_hh(
                                    page,
                                    resume,
                                    photo_file,
                                    False,
                                    before_click=up_attempt.before_click,
                                )
                            except BaseException as exc:
                                up_attempt.interrupt(exc)
                                raise
                            up_attempt.finish(up)
                            _report(
                                f"фото {path.name}",
                                up.success,
                                up.reason or "загружено и привязано",
                                uncertain=up.uncertain,
                                problems=problems,
                            )
                elif payload.get("photos") and not args.no_photos:
                    print("[WARN] фото: переносимых файлов нет — пропущено")

                # Финальная сверка: повторное чтение созданного резюме тем же
                # read-путём, что и экспорт (read-only, без фото).
                verification = export_resume_on_hh(
                    context, resume, output_dir=args.output, with_photos=False
                )
                if not verification.success:
                    print(f"[FAIL] сверка: контрольное чтение не удалось ({verification.reason})")
                    problems.append("сверка: контрольное чтение не удалось")
                else:
                    discrepancies += diff_export(payload, verification.payload)
                    if discrepancies:
                        for line in discrepancies:
                            print(f"[WARN] расхождение: {line}")
                    else:
                        print(
                            "[OK] сверка: переносимые секции совпали "
                            "(роль/фото/контакты — см. вердикты выше)"
                        )
            except AntiBotChallengeDetected as exc:
                print(f"[FAIL] браузерный сбой: {str(exc).splitlines()[0][:300]}")
                return True
            finally:
                page.close()
        print(f"[INFO] честные пропуски: {len(unavailable)}")
        for note in unavailable:
            print(f"[WARN] не переносится: {note}")
        return bool(problems)

    return run_supervised_command(
        command=getattr(args, "command", "import-resume"),
        history=history,
        requested_limit=None,
        body=_body,
    )

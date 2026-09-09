"""Сервис боевого импорта резюме: создание, секции, итоговая сверка (#1049).

Вынесено из ``commands/import_resume.py::_run_live`` (участок 6 аудита
#1035). CLI-аргументы, планирование экспорта и dry-run остаются в команде;
здесь — только исполнение боевого прогона.

НЕ универсальный runner: состав секций и их durable-границы фиксированы
этим модулем. ``before_click`` (uncertain-ledger) передаётся ТОЛЬКО
``create_resume_on_hh`` и ``upload_photo_on_hh`` — только эти функции его
принимают; расширение durable-защиты остальных секций — отдельное
изменение поведения (не здесь). Секции позиция/о себе/опыт/образование/
навыки/языки ведут себя как их одиночные команды: durable-маркера у них
нет, сбой посреди клика классифицируется вердиктами секций, и
``has_unresolved_uncertain`` их повтор не блокирует.

Разделение ответственности (#1049): адаптеры секций (``_save_*``) держат
браузерные вызовы и специфические преобразования результатов
(bool / список строк / success+acted+uncertain) в :class:`SectionOutcome`;
общий цикл ``run_import`` — только учёт (durable-попытки) и отчёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SectionOutcome:
    """Нормализованный вердикт одной секции импорта.

    ``ledger`` — исходный структурный результат для
    :meth:`DurableMutationAttempt.finish` (bool / список строк / объект с
    ``success``/``uncertain``/``acted``): durable-классификация читает его,
    а не нормализованные поля, чтобы батчи схлопывались правилами
    ``ApplyProgress``, а не вторым дублером логики.
    """

    ok: bool
    message: str
    uncertain: bool = False
    ledger: Any = field(default=True)


@dataclass
class ImportRunParams:
    """Всё, что оркестрации нужно от команды, кроме history/progress."""

    payload: dict
    position_plan: Any
    experience_plan: Any
    education_plan: Any
    skills: list
    languages: list
    photos: list[Path]
    unavailable: list[str]
    no_photos: bool
    storage_state_file: str
    headless: bool
    user_agent: str | None
    output: Path


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


# --- Адаптеры секций: браузерный вызов + специфическое преобразование результата.


def _save_position(page, resume, plan) -> SectionOutcome:  # noqa: ANN001 - Page/ResumeConfig
    """Позиция — строго editor-режим (#881): НЕ hh.ru-копия."""
    from ..resume_position import apply_position, click_save_and_wait, open_position_form

    flow = open_position_form(page, resume, enter_wizard=False)
    if flow.kind != "editor":
        raise RuntimeError(
            "форма позиция открылась не в editor-режиме — заполнение через hh.ru-копию запрещено"
        )
    apply_position(page, plan, current=flow.values)
    click_save_and_wait(page)
    return SectionOutcome(True, "сохранена в editor-режиме")


def _save_about(page, resume, text: str) -> SectionOutcome:  # noqa: ANN001 - Page/ResumeConfig
    """О себе; отказ генерации — [FAIL] секции, прогон продолжается."""
    from ..about import AboutGenerationError, open_about_editor, save_about

    try:
        open_about_editor(page, resume)
        save_about(page, text)
    except AboutGenerationError as exc:
        return SectionOutcome(False, str(exc), ledger=False)
    return SectionOutcome(True, "сохранено")


def _save_experience(page, new_id: str, plan, title: str) -> SectionOutcome:  # noqa: ANN001 - Page
    """Опыт (append-only: у нового резюме записей нет).

    resume_titles — ПОЛНЫЙ map резюме целевого аккаунта (#782): второй+
    запись идёт через shared-profile панель «Резюме с этим местом работы»,
    где все резюме стартуют pre-checked; неполный map дал бы other_titles=[]
    и молчаливую привязку импорта ко ВСЕМ резюме аккаунта (#1028 review).
    """
    from ..experience import edit_experience_on_hh

    resume_titles = _read_resume_titles(page, new_id, title)
    if resume_titles is None:
        raise RuntimeError(
            "список резюме целевого аккаунта не прочитан — "
            "опыт не переносится (риск over-binding #782)"
        )
    rows = edit_experience_on_hh(
        page,
        new_id,
        plan,
        dry_run=False,
        resume_titles=resume_titles,
        append_only=True,
    )
    return _rows_outcome(rows, empty_message=f"{len(rows)} записей")


def _save_education(page, resume_url: str, plan) -> SectionOutcome:  # noqa: ANN001 - Page
    """Образование (только primary: экспорт уровня не различает)."""
    from ..resume_education import edit_education_on_hh

    rows = edit_education_on_hh(page, resume_url, plan, section="primary", dry_run=False)
    return _rows_outcome(rows, empty_message=f"{len(plan.primary)} записей")


def _save_skills(page, resume, skills) -> SectionOutcome:  # noqa: ANN001 - Page/ResumeConfig
    """Навыки (уровень — конвенция IMPORT_SKILL_LEVEL, печатается)."""
    from ..import_resume import IMPORT_SKILL_LEVEL
    from ..skills import edit_skills_on_hh

    sk = edit_skills_on_hh(page, resume, tuple(skills), dry_run=False, mode="append")
    return SectionOutcome(
        sk.success,
        sk.reason or f"{len(sk.added or skills)} добавлено (уровень {IMPORT_SKILL_LEVEL})",
        ledger=sk,
    )


def _save_languages(page, resume, languages) -> SectionOutcome:  # noqa: ANN001 - Page/ResumeConfig
    """Языки (аккаунт-уровень на hh.ru); acted без успеха — uncertain."""
    from ..languages import edit_languages_on_hh

    lg = edit_languages_on_hh(page, resume, tuple(languages), dry_run=False, mode="append")
    return SectionOutcome(
        lg.success,
        lg.reason or f"{len(languages)} добавлено",
        uncertain=lg.acted and not lg.success,
        ledger=lg,
    )


def _rows_outcome(rows, *, empty_message: str) -> SectionOutcome:  # noqa: ANN001
    """Вердикт секции с построчным результатом (опыт/образование)."""
    bad = [r.reason for r in rows if not r.success]
    return SectionOutcome(
        not bad,
        "; ".join(bad[:3]) or empty_message,
        uncertain=any(r.uncertain for r in rows),
        ledger=rows,
    )


def _upload_photos(page, resume, params, attempt, *, problems) -> None:  # noqa: ANN001
    """Фото: лимит галереи проверяется ДО первой загрузки.

    Гейт нечитаемого инвентаря — по inventory.success, НЕ по reason:
    успешный dry-run select_photo_on_hh всегда несёт непустой reason (текст
    плана), а пустая галерея свежего аккаунта — photos=() при success=True
    (ревью hhru-496). ``attempt`` — фабрика durable-попытки: только здесь
    (как и у создания) вызываемая функция принимает ``before_click``.
    """
    from ..import_resume import GALLERY_LIMIT
    from ..resume_photo import select_photo_on_hh, upload_photo_on_hh, validate_photo

    inventory = select_photo_on_hh(page, resume, None, True)
    if not inventory.success:
        print(
            "[FAIL] фото: инвентарь галереи не прочитан "
            f"({inventory.reason}) — загрузка отменена целиком"
        )
        problems.append("фото: инвентарь галереи не прочитан")
        return
    existing = len(inventory.photos)
    free = GALLERY_LIMIT - existing
    if free <= 0:
        print(
            "[FAIL] фото: галерея аккаунта полна "
            f"({existing}/{GALLERY_LIMIT}) — ни одно фото не перенесено. "
            "План: освободите слоты в галерее вручную и перенесите фото "
            "отдельным запуском upload-photo."
        )
        problems.append("фото: галерея переполнена, отказ с планом")
        return
    fitting, overflow = params.photos[:free], params.photos[free:]
    if overflow:
        print(
            f"[WARN] фото: свободных слотов {free}, файлов {len(params.photos)} — "
            f"{len(overflow)} не поместятся "
            f"({', '.join(p.name for p in overflow)}). Освободите слоты и "
            "перенесите их отдельным запуском upload-photo."
        )
    new_id = resume.id
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


def _final_verification(context, resume, params, discrepancies) -> bool:  # noqa: ANN001
    """Финальная сверка: повторное чтение созданного резюме тем же read-путём,
    что и экспорт (read-only, без фото)."""
    from ..export_resume import export_resume_on_hh
    from ..import_resume import diff_export

    verification = export_resume_on_hh(context, resume, output_dir=params.output, with_photos=False)
    if not verification.success:
        print(f"[FAIL] сверка: контрольное чтение не удалось ({verification.reason})")
        return False
    discrepancies += diff_export(params.payload, verification.payload)
    if discrepancies:
        for line in discrepancies:
            print(f"[WARN] расхождение: {line}")
    else:
        print("[OK] сверка: переносимые секции совпали (роль/фото/контакты — см. вердикты выше)")
    return True


# --- Оркестрация: учёт durable-попыток и отчёт; состав секций фиксирован.


def _run_section(name, resume_id, action, browser, *, history, progress, problems) -> None:
    """Одна секция: durable-попытка вокруг адаптера, отчёт по вердикту."""
    from .supervision import DurableMutationAttempt

    attempt = DurableMutationAttempt(history, progress, resume_id, action)
    try:
        outcome = browser()
    except BaseException as exc:
        attempt.interrupt(exc)
        raise
    attempt.finish(outcome.ledger)
    _report(name, outcome.ok, outcome.message, uncertain=outcome.uncertain, problems=problems)


def run_import(progress, history, params: ImportRunParams) -> bool:  # noqa: ANN001
    """Боевой импорт; каждая секция — отдельная durable-попытка и вердикт."""
    from ..apply.antibot import AntiBotChallengeDetected
    from ..browser import launch_context
    from ..catalog_preflight import preflight_profession
    from ..config import bare_resume
    from ..create_resume import apply_draft_readback, create_resume_on_hh
    from .copy_resume import format_config_snippet
    from .supervision import DurableMutationAttempt

    problems: list[str] = []
    discrepancies: list[str] = []

    def attempt(resume_id: str, action: str) -> DurableMutationAttempt:
        return DurableMutationAttempt(history, progress, resume_id, action)

    with launch_context(
        params.storage_state_file, headless=params.headless, user_agent=params.user_agent
    ) as context:
        page = context.new_page()
        try:
            # Read-only сверка area с live-каталогом ДО любого клика (#950).
            outcome = preflight_profession(
                page, params.position_plan.title, allow_unresolved_area=False
            )
            if not outcome.ok:
                print(f"[FAIL] создание: {outcome.message}")
                return True
            if outcome.message:
                print(f"[WARN] {outcome.message}")
            create_attempt = attempt("account", "create_resume")
            try:
                result = create_resume_on_hh(
                    page,
                    area=params.position_plan.title,
                    title=params.position_plan.title,
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
                print(
                    "[INFO] Подсказка: если резюме с таким title уже создано "
                    "предыдущим (оборванным) прогоном, импорт не стартует с нуля — "
                    "дозаполните существующее резюме отдельными edit-командами "
                    "(resume-position, edit-experience, edit-education, edit-skills, "
                    "edit-languages, about, upload-photo)."
                )
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

            _run_section(
                "позиция",
                new_id,
                "edit_position",
                lambda: _save_position(page, resume, params.position_plan),
                history=history,
                progress=progress,
                problems=problems,
            )

            about_text = params.payload.get("about")
            if not about_text:
                print("[WARN] о себе: в экспорте пусто — пропущено")
            else:
                _run_section(
                    "о себе",
                    new_id,
                    "edit_about",
                    lambda: _save_about(page, resume, about_text),
                    history=history,
                    progress=progress,
                    problems=problems,
                )

            if params.experience_plan.entries:
                _run_section(
                    "опыт",
                    new_id,
                    "edit_experience",
                    lambda: _save_experience(
                        page, new_id, params.experience_plan, params.position_plan.title
                    ),
                    history=history,
                    progress=progress,
                    problems=problems,
                )
            else:
                print("[WARN] опыт: переносимых записей нет — пропущено")

            if params.education_plan.primary:
                _run_section(
                    "образование",
                    new_id,
                    "edit_education",
                    lambda: _save_education(page, resume.resume_url, params.education_plan),
                    history=history,
                    progress=progress,
                    problems=problems,
                )
            else:
                print("[WARN] образование: переносимых записей нет — пропущено")

            if params.skills:
                _run_section(
                    "навыки",
                    new_id,
                    "edit_key_skills",
                    lambda: _save_skills(page, resume, params.skills),
                    history=history,
                    progress=progress,
                    problems=problems,
                )
            else:
                print("[WARN] навыки: переносимых нет — пропущено")

            if params.languages:
                _run_section(
                    "языки",
                    new_id,
                    "edit_languages",
                    lambda: _save_languages(page, resume, params.languages),
                    history=history,
                    progress=progress,
                    problems=problems,
                )
            else:
                print("[WARN] языки: переносимых нет — пропущено")

            if params.photos:
                _upload_photos(page, resume, params, attempt, problems=problems)
            elif params.payload.get("photos") and not params.no_photos:
                print("[WARN] фото: переносимых файлов нет — пропущено")

            if not _final_verification(context, resume, params, discrepancies):
                problems.append("сверка: контрольное чтение не удалось")
        except AntiBotChallengeDetected as exc:
            print(f"[FAIL] браузерный сбой: {str(exc).splitlines()[0][:300]}")
            return True
        finally:
            page.close()
    print(f"[INFO] честные пропуски: {len(params.unavailable)}")
    for note in params.unavailable:
        print(f"[WARN] не переносится: {note}")
    return bool(problems)

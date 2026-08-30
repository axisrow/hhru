"""Применить ``AdaptiveResumeContent`` на резюме hh.ru (issue #769, PR-2 эпика #750).

PR-1 (#753, ``adaptive_resume.py``) уже генерирует содержимое; этот модуль —
тонкая оркестрация существующих переиспользуемых WRITE-путей (CLAUDE.md
"Максимальная простота + переиспользование" — не пишет новый HTTP/DOM-код):

- ``title`` -> ``resume_position.py`` (``open_position_form``/``apply_position``),
  тот же non-wizard путь, что и ``resume-position --title`` без LLM-планирования.
- ``about`` -> ``about.py::save_about`` поверх уже открытого редактора
  (``open_about_editor``).
- ``skills`` -> ``skills.py::edit_skills_on_hh`` в режиме ``append`` — hh.ru не
  даёт клиенту переиспользуемого пути ПЕРЕУПОРЯДОЧИТЬ существующие чипы
  навыков (``edit_skills_on_hh`` только добавляет отсутствующие, см. его
  реализацию), поэтому применяется приоритетный список ``content.skills``
  как набор добавляемых навыков — фактическое переупорядочивание вне
  досягаемости существующего WRITE-пути и не эмулируется этим модулем.

**work_experience и projects сознательно вне скоупа этого PR** (зафиксировано
в теле issue #769 как допустимое сужение первой версии). Причина — не
отсутствие переиспользуемого пути записи, а его подтверждённая опасность:
разведка #787 (закрыта 2026-08-30) установила, что форма опыта работы hh.ru
живёт в общем профиле (``/profile/edit/experience?resumeFrom={resume_id}``,
тот же shared-editor shape, что подтверждён в #840) и несёт панель «Резюме с
этим местом работы» — список чекбоксов по одному на КАЖДОЕ резюме аккаунта,
определяющий, к каким резюме привяжется запись. Для НОВОЙ записи (создание
через ``EXPERIENCE_ADD_BUTTON``) **все чекбоксы аккаунта отмечены по
умолчанию**, включая опубликованные резюме, а часть строк списка физически
отсутствует в DOM до клика "Развернуть" (не просто скрыта CSS, чекбокс
локатором недостижим). WRITE без явного снятия лишних чекбоксов ПЕРЕД save —
это не молчаливый no-op (как предполагалось до разведки), а **молчаливая
порча** (over-binding): запись опыта тихо привязывается ко всем резюме
аккаунта разом, включая опубликованные, которые CLAUDE.md прямо запрещает
трогать. Рецепт безопасного WRITE (раскрыть список, снять чекбоксы всех
резюме кроме целевого по `input[aria-label="<title резюме>"]`, пост-save
маркер — состояние чекбокса целевого резюме) зафиксирован в комментарии
к #787; это отдельная фича (не голый reuse существующего
``edit_experience_on_hh``, у него такой панели ещё нет), поэтому реализация
— follow-up, а не часть этого PR.

Каждый шаг (title/about/skills) применяется и отчитывается НЕЗАВИСИМО:
частичный успех виден по каждому шагу отдельным ``[OK]``/``[FAIL]``, ни один
шаг не глушит соседний (issue #769 "Fail-closed" ограничение).

**Over-binding риск #787 этот PR не затрагивает.** title/about/skills — это
свойства КОНКРЕТНОГО резюме (``resume_position.py``/``about.py``/``skills.py``
все адресуются по ``resume.resume_id`` и открывают resume-scoped роуты), а не
общего профиля аккаунта — ни один из трёх шагов не открывает
``/profile/edit/experience`` или любую другую страницу с панелью «Резюме с
этим местом работы». Риск специфичен для формы опыта работы (см. выше) и не
переносится на title/about/skills.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .adaptive_resume import AdaptiveResumeContent
    from .config import ResumeConfig

#: Шаги, которые эта версия применяет на hh.ru. work_experience/projects
#: сознательно исключены — см. module docstring.
APPLIED_STEPS: tuple[str, ...] = ("title", "about", "skills")


@dataclass(frozen=True)
class StepResult:
    """Структурный исход одного шага применения (title/about/skills)."""

    step: str
    success: bool = False
    uncertain: bool = False
    skipped: bool = False
    acted: bool = False
    reason: str = ""


def _apply_title(page: Page, resume: ResumeConfig, title: str, *, dry_run: bool) -> StepResult:
    """Только non-wizard путь (та же граница, что у ``resume-position --title``
    без LLM): визард запрашивает подтверждённую классификацию профессии
    (``role_id``), которую этот модуль не строит — вне скоупа adaptive-resume.
    """
    from .resume_position import CANCEL, SAVE, PositionValues, apply_position, open_position_form

    try:
        flow = open_position_form(page, resume)
    except Exception as exc:  # noqa: BLE001 - переводим в структурный результат
        return StepResult("title", reason=f"не удалось открыть форму позиции: {exc}")
    if flow.kind == "wizard":
        return StepResult(
            "title",
            reason=(
                "раздел желаемой работы резюме — незаполненный визард "
                "professional_role; adaptive-resume apply title не поддерживает "
                "визард (используйте resume-position для первичного заполнения)"
            ),
        )
    if flow.values.title == title:
        return StepResult("title", success=True, reason="желаемая должность уже совпадает")
    try:
        apply_position(page, PositionValues(title=title), current=flow.values)
    except (ValueError, RuntimeError) as exc:
        return StepResult("title", reason=str(exc))
    if dry_run:
        page.locator(CANCEL).click()
        return StepResult("title", success=True, reason="предложено, save не нажат")
    if page.locator(SAVE).count() != 1:
        return StepResult("title", reason="кнопка сохранения формы позиции не подтверждена")
    try:
        page.locator(SAVE).click()
        page.locator("[data-qa='resume-edit-position-form']").wait_for(
            state="hidden", timeout=10_000
        )
    except PlaywrightError as exc:
        return StepResult(
            "title", acted=True, uncertain=True, reason=f"сохранение не подтверждено: {exc}"
        )
    return StepResult("title", success=True, acted=True, reason="желаемая должность сохранена")


def _apply_about(page: Page, resume: ResumeConfig, about: str, *, dry_run: bool) -> StepResult:
    from .about import AboutGenerationError, open_about_editor, save_about

    try:
        existing = open_about_editor(page, resume)
    except AboutGenerationError as exc:
        return StepResult("about", reason=str(exc))
    if existing.strip() == about.strip():
        return StepResult("about", success=True, reason="текст «Обо мне» уже совпадает")
    if dry_run:
        return StepResult("about", success=True, reason="предложено, save не нажат")
    try:
        save_about(page, about)
    except AboutGenerationError as exc:
        # save_about формулирует post-click grey-zone как "не подтверждено
        # (uncertain)" в самом тексте (CLAUDE.md #207) — тот же дискриминатор,
        # что уже используют about.py/commands/about.py.
        uncertain = "uncertain" in str(exc)
        return StepResult("about", acted=uncertain, uncertain=uncertain, reason=str(exc))
    return StepResult("about", success=True, acted=True, reason="«Обо мне» сохранён")


def _apply_skills(
    page: Page, resume: ResumeConfig, skills: tuple[str, ...], *, dry_run: bool
) -> StepResult:
    from .skills import Skill, edit_skills_on_hh

    if not skills:
        return StepResult("skills", skipped=True, reason="кластер не предложил навыков")
    proposed = tuple(Skill(name, "intermediate") for name in skills)
    result = edit_skills_on_hh(page, resume, proposed, dry_run=dry_run, mode="append")
    if not result.success:
        return StepResult(
            "skills", acted=result.acted, uncertain=result.acted, reason=result.reason
        )
    if dry_run:
        return StepResult(
            "skills", success=True, reason=f"будет добавлено: {', '.join(result.added) or 'ничего'}"
        )
    return StepResult(
        "skills",
        success=True,
        acted=True,
        reason=f"добавлено: {', '.join(result.added) or 'ничего (все навыки уже были)'}",
    )


def apply_adaptive_resume(
    page: Page,
    resume: ResumeConfig,
    content: AdaptiveResumeContent,
    *,
    dry_run: bool,
) -> tuple[StepResult, ...]:
    """Применить title/about/skills из ``content`` на резюме hh.ru.

    Шаги применяются последовательно и независимо: неудача одного не
    прерывает следующий (fail-closed видимость по каждому шагу, не единый
    непрозрачный результат — issue #769). Каждый вызов открывает нужный
    редактор заново (``open_position_form``/``open_about_editor``/
    ``edit_skills_on_hh`` сами делают ``goto_hh`` на страницу резюме), поэтому
    порядок шагов не создаёт зависимостей между их DOM-состояниями.
    """
    return (
        _apply_title(page, resume, content.title, dry_run=dry_run),
        _apply_about(page, resume, content.about, dry_run=dry_run),
        _apply_skills(page, resume, content.skills, dry_run=dry_run),
    )


__all__ = [
    "APPLIED_STEPS",
    "StepResult",
    "apply_adaptive_resume",
]

"""LLM planning and safe browser editing of resume key skills (#263)."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    HH_BASE_URL,
    goto_hh,
    has_auth_cookie,
    has_login_form,
    open_hydrated_resume_editor,
)
from .config import ResumeConfig
from .selector_groups import resume_page

logger = logging.getLogger("hhru_bot.skills")

LEVELS = frozenset(("basic", "intermediate", "advanced"))
EDITOR_MOUNT_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class Skill:
    name: str
    level: str


@dataclass(frozen=True)
class SkillsResult:
    success: bool
    existing: tuple[str, ...] = ()
    proposed: tuple[Skill, ...] = ()
    added: tuple[str, ...] = ()
    reason: str = ""
    acted: bool = False


def parse_skill_plan(content: str) -> tuple[Skill, ...]:
    """Parse a strict LLM JSON list, rejecting unsafe/ambiguous output."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ValueError("LLM вернул незакрытый JSON code fence")
        text = "\n".join(lines[1:-1]).strip()
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM вернул невалидный JSON") from exc
    if not isinstance(raw, list):
        raise ValueError("LLM должен вернуть JSON-массив навыков")
    result: list[Skill] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "level"}:
            raise ValueError("каждый навык должен иметь только поля name и level")
        name = item["name"]
        level = item["level"]
        if not isinstance(name, str) or not name.strip() or not isinstance(level, str):
            raise ValueError("имя навыка и уровень должны быть непустыми строками")
        normalized = " ".join(name.split())
        key = normalized.casefold()
        if level not in LEVELS:
            raise ValueError(f"неподдерживаемый уровень навыка: {level!r}")
        if key in seen:
            raise ValueError(f"дублирующийся навык: {normalized}")
        seen.add(key)
        result.append(Skill(normalized, level))
    return tuple(result)


def parse_manual_skills(values: list[str]) -> tuple[Skill, ...]:
    """Parse repeatable ``NAME=LEVEL`` CLI values."""
    return (
        parse_skill_plan(
            json.dumps(
                [
                    {
                        "name": value.rsplit("=", 1)[0].strip(),
                        "level": value.rsplit("=", 1)[1].strip(),
                    }
                    for value in values
                    if "=" in value
                ]
            )
        )
        if all("=" in value for value in values)
        else _raise_manual()
    )


def _raise_manual() -> tuple[Skill, ...]:
    raise ValueError("ручной навык задаётся как NAME=basic|intermediate|advanced")


def build_skills_prompt(
    page_text: str, existing: tuple[str, ...], mode: str
) -> list[dict[str, str]]:
    system = (
        "Верни только JSON-массив объектов с ровно полями name и level. "
        "level обязан быть basic, intermediate или advanced. Не выдумывай навыки."
    )
    task = "с нуля" if mode == "fresh" else "до-заполнения, сохраняя существующие"
    user = (
        f"Режим: {task}. Уже есть: {json.dumps(existing, ensure_ascii=False)}.\n"
        "Извлеки релевантные ключевые навыки из текста резюме и предложи максимум 20.\n"
        f"Текст резюме:\n{page_text[:12000]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def read_skills(page: Page) -> tuple[str, ...]:
    chips = page.locator(resume_page.RESUME_SKILLS_CHIP)
    count = chips.count()
    if count == 0:
        return ()
    values = []
    for i in range(count):
        value = chips.nth(i).inner_text().strip()
        if value:
            values.append(value)
    return tuple(values)


def _skill_key(name: str) -> str:
    """Casefolded, whitespace-normalized key for comparing skill chips.

    parse_skill_plan normalizes planned names via ``" ".join(name.split())``;
    read_skills only strips. Without applying the same normalization to the
    observed/existing chips, a chip rendered with a double space or nbsp would
    falsely mismatch the Counter and report false uncertain, locking the resume
    via has_unresolved_uncertain (#536 round 2). The raw chip spelling is still
    preserved in the success report; this key is only for equality comparison.
    """
    return " ".join(name.split()).casefold()


def edit_skills_on_hh(
    page: Page,
    resume: ResumeConfig,
    skills: tuple[Skill, ...],
    *,
    dry_run: bool,
    mode: str,
) -> SkillsResult:
    """Open the confirmed skills editor; save only when the caller authorized it."""
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume.resume_id}")
    if not has_auth_cookie(page) or has_login_form(page):
        return SkillsResult(False, reason="сессия hh.ru не подтверждена")
    edit_path = f"/resume/edit/{resume.resume_id}/keySkills"
    trigger = page.locator(resume_page.RESUME_SKILLS_EDIT_BUTTON)
    first_entry = trigger.count() == 0
    if first_entry:
        # #789/#787: on a resume with zero skills the regular edit button is
        # absent. The visible suggestion chip navigates to the *shared*
        # profile editor (/profile/edit/keySkills) without a reliable
        # resumeFrom binding on an incomplete resume (#787 live
        # confirmation: the query param was dropped entirely). The
        # resume-scoped route below was confirmed live (#787 write test)
        # to open the form directly, pre-bound to this resume_id, with
        # no click or checkbox panel involved.
        try:
            goto_hh(page, f"{HH_BASE_URL}{edit_path}")
        except PlaywrightError as exc:
            return SkillsResult(
                False,
                reason=f"форма навыков не открылась: {exc}",
            )
        if urlsplit(page.url).path.rstrip("/") != edit_path:
            return SkillsResult(
                False,
                reason=f"форма навыков открыта не для того резюме ({page.url})",
            )
        editor = page.locator(resume_page.RESUME_SKILLS_INPUT)
        try:
            editor.wait_for(state="visible", timeout=EDITOR_MOUNT_TIMEOUT_MS)
        except PlaywrightError as exc:
            return SkillsResult(
                False,
                reason=f"форма навыков не открылась: {exc}",
            )
    else:
        try:
            editor = open_hydrated_resume_editor(
                page,
                trigger_selector=trigger,
                editor_selector=resume_page.RESUME_SKILLS_INPUT,
                profile_path=f"/resume/{resume.resume_id}",
                edit_path=edit_path,
                click_trigger=True,
                timeout=EDITOR_MOUNT_TIMEOUT_MS,
                trigger_error="кнопка редактирования навыков не найдена однозначно",
                open_error="форма навыков не открылась",
                wrong_route_error="форма навыков открыта не для того резюме",
            )
        except RuntimeError as exc:
            return SkillsResult(False, reason=str(exc))
    existing = read_skills(page)
    existing_keys = {_skill_key(skill) for skill in existing}
    additions = tuple(skill for skill in skills if _skill_key(skill.name) not in existing_keys)
    if mode == "fresh" and existing:
        return SkillsResult(False, existing, skills, reason="режим с нуля требует пустого раздела")
    if dry_run:
        page.locator(resume_page.RESUME_PARTIAL_EDIT_CANCEL).click()
        return SkillsResult(True, existing, skills, tuple(s.name for s in additions))
    input_ = page.locator(resume_page.RESUME_SKILLS_CHIP_INPUT)
    if input_.count() != 1:
        return SkillsResult(
            False, existing, skills, reason="поле ввода навыка не найдено однозначно"
        )
    for skill in additions:
        input_.fill(skill.name)
        input_.press("Enter")
    save = page.locator(resume_page.RESUME_PARTIAL_EDIT_SAVE)
    if save.count() != 1:
        return SkillsResult(
            False, existing, skills, reason="кнопка сохранения не найдена однозначно"
        )
    try:
        save.click()
        # The editor disappearing is the only confirmed positive UI signal for
        # this inline mutation.  A timeout is deliberately reported as
        # uncertain: the click may already have reached hh.ru.
        editor.wait_for(state="hidden", timeout=30_000)
    except PlaywrightError as exc:
        return SkillsResult(
            False,
            existing,
            skills,
            tuple(s.name for s in additions),
            reason=f"сохранение навыков не подтверждено: {exc}",
            acted=True,
        )
    # editor.wait_for(state="hidden") only confirms the overlay closed, not that
    # the underlying resume page has re-rendered the chip list (CLAUDE.md: "commit
    # не значит отрисовано"). Give the chip count a short window to settle before
    # the strict read below, matching the wait_for(state="visible") pattern used
    # after other mutating clicks (resume_position.py, bump.py, etc.) — a mismatch
    # is still fail-closed after the wait, this only avoids racing the re-render.
    expected_chip_count = len(existing) + len(additions)
    if expected_chip_count > 0:
        try:
            page.locator(resume_page.RESUME_SKILLS_CHIP).nth(expected_chip_count - 1).wait_for(
                state="visible", timeout=5_000
            )
        except PlaywrightError:
            pass
    try:
        observed = read_skills(page)
    except PlaywrightError as exc:
        return SkillsResult(
            False,
            existing,
            skills,
            reason=f"сохранение навыков не подтверждено: чипы не прочитаны: {exc}",
            acted=True,
        )

    # The editor closing only confirms that the UI accepted the interaction;
    # the chips are the source of truth for what actually landed on the resume.
    # Compare multisets so a rejected, duplicated, or otherwise unexpected
    # chip cannot be reported as a successful addition.
    existing_keys = [_skill_key(skill) for skill in existing]
    expected_keys = existing_keys + [_skill_key(skill.name) for skill in additions]
    observed_keys = [_skill_key(skill) for skill in observed]
    if Counter(observed_keys) != Counter(expected_keys):
        return SkillsResult(
            False,
            existing,
            skills,
            reason=(
                "сохранение навыков не подтверждено: наблюдаемое состояние чипов "
                f"не совпало с планом (ожидалось {len(expected_keys)}, "
                f"наблюдалось {len(observed_keys)})"
            ),
            acted=True,
        )

    # Preserve the spelling observed on hh.ru in the success report while
    # keeping ``existing`` as the pre-write snapshot used for the "было" count.
    remaining_existing = Counter(existing_keys)
    observed_added: list[str] = []
    for skill in observed:
        key = _skill_key(skill)
        if remaining_existing[key]:
            remaining_existing[key] -= 1
        else:
            observed_added.append(skill)
    return SkillsResult(True, existing, skills, tuple(observed_added), acted=True)

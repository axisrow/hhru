"""LLM planning and safe browser editing of resume key skills (#263)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import HH_BASE_URL, goto_hh, has_auth_cookie, has_login_form
from .config import ResumeConfig
from .selector_groups import resume_page

logger = logging.getLogger("hhru_bot.skills")

LEVELS = frozenset(("basic", "intermediate", "advanced"))


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


def edit_skills_on_hh(
    page: Page,
    resume: ResumeConfig,
    skills: tuple[Skill, ...],
    *,
    dry_run: bool,
    mode: str,
) -> SkillsResult:
    """Open the confirmed inline editor; save only when the caller authorized it."""
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume.resume_id}")
    if not has_auth_cookie(page) or has_login_form(page):
        return SkillsResult(False, reason="сессия hh.ru не подтверждена")
    trigger = page.locator(resume_page.RESUME_SKILLS_EDIT_BUTTON)
    if trigger.count() != 1:
        return SkillsResult(False, reason="кнопка редактирования навыков не найдена однозначно")
    trigger.click()
    editor = page.locator(resume_page.RESUME_SKILLS_INPUT)
    try:
        editor.wait_for(state="visible")
    except PlaywrightError:
        return SkillsResult(False, reason="inline-форма навыков не открылась")
    existing = read_skills(page)
    existing_keys = {skill.casefold() for skill in existing}
    additions = tuple(skill for skill in skills if skill.name.casefold() not in existing_keys)
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
    return SkillsResult(True, existing, skills, tuple(s.name for s in additions), acted=True)

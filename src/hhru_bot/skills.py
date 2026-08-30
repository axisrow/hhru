"""LLM planning and safe browser editing of resume key skills (#263)."""

from __future__ import annotations

import json
import logging
import re
import time
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
# #813: the Russian labels hh.ru's skillsLevels wizard step uses as the tail
# half of each level radio's `name` attribute (confirmed live 2026-08-30,
# `input[name='SeleniumСредний']` etc.). This is UI text, not a data-qa value,
# so it is kept local to this module rather than in selector_groups/ — it is
# not a CSS selector fragment, just a string this module concatenates into one.
_LEVEL_LABELS = {"basic": "Базовый", "intermediate": "Средний", "advanced": "Продвинутый"}
SKILLS_LEVELS_STEP_TIMEOUT_MS = 15_000
# #801: the skill chip input is an autocomplete combobox. A blind fill+Enter
# for the next skill can race the browser's handling of the previous one and
# concatenate two skill names into a single chip instead of creating two
# separate chips. CHIP_COMMIT_TIMEOUT_MS bounds how long each iteration waits
# for a positive signal (an exact-text chip appeared AND the input cleared)
# before moving on; CHIP_COMMIT_POLL_MS is the poll interval, matching the
# poll-loop pattern already used for a similar input-clear wait in
# resume_position.py.
CHIP_COMMIT_TIMEOUT_MS = 5_000
CHIP_COMMIT_POLL_MS = 100


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
    """Read the chips inside the still-open keySkills editor (form scope only).

    ``RESUME_SKILLS_CHIP`` (``chips-trigger-chip-*``) is the combobox widget's
    own markup and only exists while the editor is mounted. Do not use this
    for a post-save check: saving closes the editor and returns the page to
    the resume card, where this selector always observes zero chips
    regardless of what actually saved (#813). Use ``read_display_skills`` for
    that.
    """
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


def read_display_skills(page: Page) -> tuple[str, ...]:
    """Read the saved skill tags from the resume card (post-editor, #813).

    ``RESUME_SKILLS_DISPLAY_TAG`` targets the Magritte tags hh.ru renders on
    the resume itself, independent of the editor's own chip widget. This is
    the only selector that reflects what actually landed on hh.ru once the
    editor has closed.
    """
    tags = page.locator(resume_page.RESUME_SKILLS_DISPLAY_TAG)
    count = tags.count()
    if count == 0:
        return ()
    values = []
    for i in range(count):
        value = tags.nth(i).inner_text().strip()
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


def _confirm_skill_levels(page: Page, additions: tuple[Skill, ...]) -> str | None:
    """Confirm the levels wizard step hh.ru inserts after saving new skills.

    Saving the keySkills editor with at least one skill lacking a confirmed
    level does not return to the resume card directly: hh.ru navigates to a
    second step, ``/resume/edit/{id}/skillsLevels?fromBlock=keySkills``, with
    one radio group per pending skill (#813, confirmed live 2026-08-30). Not
    every ``additions`` name is guaranteed to appear there — a duplicate
    already-known-elsewhere skill can be silently absorbed — so each radio
    click is best-effort and a missing one is not itself a failure; the
    caller's post-save Counter check is what stays fail-closed on the result.

    Returns an error string on an unrecoverable problem (radio not found
    uniquely, second save not found/click failed), or ``None`` if this step
    was not present (first save returned straight to the resume card) or was
    handled.
    """
    edit_path_prefix = "/resume/edit/"
    if not urlsplit(page.url).path.rstrip("/").endswith("/skillsLevels"):
        return None
    if not urlsplit(page.url).path.startswith(edit_path_prefix):
        return None
    for skill in additions:
        label = _LEVEL_LABELS.get(skill.level)
        if label is None:
            continue
        radio = page.locator(
            resume_page.RESUME_SKILLS_LEVEL_RADIO_TEMPLATE.format(
                skill_and_level=f"{skill.name}{label}"
            )
        )
        if radio.count() != 1:
            # Not every addition necessarily gets its own radio group here
            # (e.g. a name hh.ru folded into an existing skill) — skip rather
            # than fail-closed on a single missing one; the final Counter
            # check downstream still catches a skill that never landed.
            continue
        try:
            radio.click(force=True, timeout=SKILLS_LEVELS_STEP_TIMEOUT_MS)
        except PlaywrightError as exc:
            return (
                f"выбор уровня навыка {skill.name!r} на экране skillsLevels не подтверждён: {exc}"
            )
    save = page.locator(resume_page.RESUME_PARTIAL_EDIT_SAVE)
    if save.count() != 1:
        return "кнопка сохранения уровней навыков не найдена однозначно"
    try:
        save.click()
        save.wait_for(state="hidden", timeout=SKILLS_LEVELS_STEP_TIMEOUT_MS)
    except PlaywrightError as exc:
        return f"сохранение уровней навыков не подтверждено: {exc}"
    return None


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
    chips = page.locator(resume_page.RESUME_SKILLS_CHIP)
    for skill in additions:
        input_.fill(skill.name)
        input_.press("Enter")
        # #801: wait for a positive commit signal before the next iteration's
        # fill() — a blind fill+Enter pair for consecutive skills can race the
        # combobox's autocomplete handling and concatenate two names into one
        # chip (e.g. "FastAPI" + "LangChain" -> "FastAPILangChain"). Checking
        # only that the chip count grew would not catch this: a merged chip
        # still increments the count by one. The positive signal is a chip
        # whose text exactly equals this skill's name AND the input cleared
        # back to empty — a timeout does not roll back (the Enter may already
        # have reached hh.ru), so it stops issuing further input and lets the
        # existing post-save Counter check fail-closed on the resulting
        # mismatch (same principle as "commit не значит отрисовано" elsewhere
        # in this codebase).
        expected_chip = chips.filter(has_text=re.compile(rf"^{re.escape(skill.name)}$"))
        deadline = time.monotonic() + CHIP_COMMIT_TIMEOUT_MS / 1000
        while time.monotonic() < deadline and (expected_chip.count() == 0 or input_.input_value()):
            page.wait_for_timeout(CHIP_COMMIT_POLL_MS)
        if expected_chip.count() == 0 or input_.input_value():
            logger.warning(
                "чип навыка %r не подтверждён за %dмс, дальнейший ввод остановлен",
                skill.name,
                CHIP_COMMIT_TIMEOUT_MS,
            )
            break
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
    # #813: a skill with no previously-confirmed level (any brand-new addition)
    # routes through a second wizard step instead of returning to the resume
    # card directly. Without handling it, the skill saves with no level at all
    # ("Уровень не указан") and the editor never reaches the resume card, so
    # the post-save Counter check below correctly — not falsely — observed a
    # mismatch: the click already reached hh.ru (acted=True), so this is
    # uncertain, not a plain failure.
    levels_error = _confirm_skill_levels(page, additions)
    if levels_error is not None:
        return SkillsResult(
            False,
            existing,
            skills,
            tuple(s.name for s in additions),
            reason=levels_error,
            acted=True,
        )
    # editor.wait_for(state="hidden") only confirms the overlay closed, not that
    # the underlying resume page has re-rendered the chip list (CLAUDE.md: "commit
    # не значит отрисовано"). Give the tag count a short window to settle before
    # the strict read below, matching the wait_for(state="visible") pattern used
    # after other mutating clicks (resume_position.py, bump.py, etc.) — a mismatch
    # is still fail-closed after the wait, this only avoids racing the re-render.
    #
    # #813: the wait (and the read below) target RESUME_SKILLS_DISPLAY_TAG, the
    # resume card's own markup, NOT RESUME_SKILLS_CHIP — that selector is the
    # editor's own combobox widget and no longer exists once the editor has
    # closed, so waiting on it/reading it here always observed zero regardless
    # of what actually saved (a selector-scope bug, not a render race: the
    # wait above already gave the DOM time to settle, and it still read 0).
    expected_chip_count = len(existing) + len(additions)
    if expected_chip_count > 0:
        try:
            page.locator(resume_page.RESUME_SKILLS_DISPLAY_TAG).nth(
                expected_chip_count - 1
            ).wait_for(state="visible", timeout=5_000)
        except PlaywrightError:
            pass
    try:
        observed = read_display_skills(page)
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

"""Generate and edit the resume's inline ``Обо мне`` section (#260).

The page and selectors deliberately live here rather than using an undocumented
HTTP endpoint.  hh.ru opens this editor inline on ``/resume/{id}``; the old
``/edit`` route returns 404 (authenticated research in #268).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import goto_hh, open_hydrated_resume_editor
from .selector_groups import resume_page

if TYPE_CHECKING:
    from .ai.llm_client import LLMClient
    from .config import ResumeConfig
    from .config_sections.ai_profile import AIProfile

logger = logging.getLogger("hhru_bot.about")
SAVE_TIMEOUT_MS = 30_000


class AboutGenerationError(RuntimeError):
    """No safe draft could be produced."""


@dataclass(frozen=True)
class AboutDraft:
    text: str
    mode: str
    source: str


def _profile_lines(profile: AIProfile | None) -> list[str]:
    if profile is None:
        return []
    lines: list[str] = []
    for label, value in (
        ("Кратко о кандидате", getattr(profile, "summary", "")),
        ("Желаемая роль", getattr(profile, "desired_role", "")),
    ):
        if value:
            lines.append(f"{label}: {value}")
    for label, values in (
        ("Навыки", getattr(profile, "skills", [])),
        ("Сильные стороны и достижения", getattr(profile, "highlights", [])),
    ):
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    return lines


def build_about_prompt(existing: str, profile: AIProfile | None) -> list[dict[str, str]]:
    """Build a conservative prompt: existing text is context, never a target to rewrite."""
    mode = "до-заполнение" if existing.strip() else "с нуля"
    system = (
        "Ты помогаешь заполнить раздел «Обо мне» резюме на hh.ru. Пиши на русском, "
        "профессионально и конкретно, без эмодзи и выдуманных фактов. Не дублируй "
        "обязанности из опыта работы, полный список достижений или портфолио. "
        "Учитывай только объяснение карьерных переходов и перерывов, текущее "
        "обучение и релевантные soft skills. Верни только готовый текст без пояснений."
    )
    lines = [f"Режим: {mode}."]
    if existing.strip():
        lines += [
            "Существующий текст нельзя переписывать или редактировать.",
            "Верни только короткий недостающий фрагмент, который можно добавить после него.",
            f"Существующий текст:\n{existing.strip()}",
        ]
    else:
        lines.append("Сформируй самопрезентацию с нуля.")
    context = _profile_lines(profile)
    if context:
        lines.append("Данные кандидата:\n" + "\n".join(context))
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(lines)}]


def generate_about(
    llm_client: LLMClient,
    existing: str,
    profile: AIProfile | None,
) -> AboutDraft:
    """Generate a draft, preserving existing text byte-for-byte in fill mode."""
    mode = "до-заполнение" if existing.strip() else "с нуля"
    try:
        response = llm_client.chat(
            build_about_prompt(existing, profile), temperature=0.5, max_tokens=500
        )
    except Exception as exc:  # noqa: BLE001 - fail closed for a write command
        logger.warning("About generation failed: %s", exc)
        fallback = _fallback_about(existing, profile, mode)
        if fallback is not None:
            return fallback
        raise AboutGenerationError(f"LLM недоступен: {exc}") from exc
    addition = (getattr(response, "content", None) or "").strip()
    if not addition:
        fallback = _fallback_about(existing, profile, mode)
        if fallback is not None:
            return fallback
        raise AboutGenerationError("LLM не вернул текст для раздела «Обо мне»")
    if existing.strip():
        return AboutDraft(f"{existing.rstrip()}\n\n{addition}", mode, "llm")
    return AboutDraft(addition, mode, "llm")


def _fallback_about(existing: str, profile: AIProfile | None, mode: str) -> AboutDraft | None:
    """Safe #17-style fallback: never invent text and never replace user text."""
    if existing.strip():
        return AboutDraft(existing, mode, "fallback")
    summary = (getattr(profile, "summary", "") if profile is not None else "").strip()
    if summary:
        return AboutDraft(summary, mode, "fallback")
    return None


def open_about_editor(page: Page, resume: ResumeConfig) -> str:
    """Open the confirmed inline editor and return its current textarea value."""
    goto_hh(page, resume.resume_url, ready_selector=resume_page.RESUME_EDIT_ABOUT_BUTTON)
    try:
        field = open_hydrated_resume_editor(
            page,
            trigger_selector=resume_page.RESUME_EDIT_ABOUT_BUTTON,
            editor_selector=resume_page.RESUME_ABOUT_EDITOR,
            profile_path=f"/resume/{resume.resume_id}",
            edit_path=re.compile(
                rf"/resume(?:/{re.escape(resume.resume_id)}|/edit/{re.escape(resume.resume_id)}/about)"
            ),
            # Pre-#339 behavior clicked unconditionally: the editor marker can
            # be present in the DOM before React hydrates it, so count() == 1
            # does not mean it is visible/functional yet. Without this, a
            # hidden-but-present field skips both the click and the
            # wait_for(visible), and input_value() silently reads "" from an
            # unhydrated textarea instead of the real "Обо мне" text.
            click_trigger=True,
            trigger_error="кнопка редактирования «Обо мне» не найдена однозначно",
            open_error="форма «Обо мне» не открылась",
            wrong_route_error="форма «Обо мне» открыта не для того резюме",
        )
    except RuntimeError as exc:
        raise AboutGenerationError(str(exc)) from exc
    return field.input_value()


def save_about(page: Page, text: str) -> None:
    """Fill and save only after the caller has obtained explicit confirmation."""
    field = page.locator(resume_page.RESUME_ABOUT_EDITOR)
    field.fill(text)
    save = page.locator(resume_page.RESUME_PARTIAL_EDIT_SAVE)
    if save.count() != 1:
        raise AboutGenerationError("кнопка сохранения «Обо мне» не найдена однозначно")
    try:
        save.click()
        field.wait_for(state="hidden", timeout=SAVE_TIMEOUT_MS)
    except PlaywrightError as exc:
        raise AboutGenerationError(
            "сохранение не подтверждено (uncertain): inline-форма не закрылась после клика"
        ) from exc

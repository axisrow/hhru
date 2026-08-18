"""Generate and edit the resume's inline ``Обо мне`` section (#260).

The page and selectors deliberately live here rather than using an undocumented
HTTP endpoint.  hh.ru opens this editor inline on ``/resume/{id}``; the old
``/edit`` route returns 404 (authenticated research in #268).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import goto_hh
from .selector_groups import resume_page

if TYPE_CHECKING:
    from .ai.llm_client import LLMClient
    from .config import ResumeConfig
    from .config_sections.ai_profile import AIProfile

logger = logging.getLogger("hhru_bot.about")


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
    trigger = page.locator(resume_page.RESUME_EDIT_ABOUT_BUTTON)
    if trigger.count() != 1:
        raise AboutGenerationError("кнопка редактирования «Обо мне» не найдена однозначно")
    trigger.click()
    field = page.locator(resume_page.RESUME_ABOUT_EDITOR)
    field.wait_for(state="visible")
    return field.input_value()


def save_about(page: Page, text: str) -> None:
    """Fill and save only after the caller has obtained explicit confirmation."""
    field = page.locator(resume_page.RESUME_ABOUT_EDITOR)
    field.fill(text)
    save = page.locator(resume_page.RESUME_PARTIAL_EDIT_SAVE)
    if save.count() != 1:
        raise AboutGenerationError("кнопка сохранения «Обо мне» не найдена однозначно")
    save.click()
    try:
        field.wait_for(state="hidden")
    except PlaywrightError as exc:
        raise AboutGenerationError("сохранение не подтверждено: inline-форма не закрылась") from exc

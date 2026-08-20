"""LLM planning and fail-closed editing for resume languages (#265).

The language level is deliberately not inferred by the model.  LLM output may
only contain a language name and a ``null`` level; a concrete CEFR value must
come from the operator (for example ``--language English=B2``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from .browser import HH_BASE_URL, goto_hh, has_auth_cookie, has_login_form
from .config import ResumeConfig
from .selector_groups import resume_page

CEFR_LEVELS = frozenset(("A1", "A2", "B1", "B2", "C1", "C2"))
CEFR_LABELS = {
    "A1": "A1 — Начальный",
    "A2": "A2 — Элементарный",
    "B1": "B1 — Средний",
    "B2": "B2 — Средне-продвинутый",
    "C1": "C1 — Продвинутый",
    "C2": "C2 — В совершенстве",
}


@dataclass(frozen=True)
class Language:
    name: str
    level: str | None = None


@dataclass(frozen=True)
class LanguagesResult:
    success: bool
    proposed: tuple[Language, ...] = ()
    reason: str = ""
    acted: bool = False


def _normalized_name(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def parse_language_plan(content: str) -> tuple[Language, ...]:
    """Parse strict JSON and reject guessed/invalid CEFR levels."""
    try:
        raw = json.loads(content.strip())
    except (AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("LLM вернул невалидный JSON") from exc
    if not isinstance(raw, list):
        raise ValueError("LLM должен вернуть JSON-массив языков")
    result: list[Language] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "level"}:
            raise ValueError("каждый язык должен иметь только поля name и level")
        name = _normalized_name(item["name"])
        level = item["level"]
        if not name:
            raise ValueError("название языка должно быть непустым")
        if level is not None and (not isinstance(level, str) or level not in CEFR_LEVELS):
            raise ValueError("уровень CEFR должен быть null или одним из A1-A2-B1-B2-C1-C2")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"дублирующийся язык: {name}")
        seen.add(key)
        result.append(Language(name, level))
    return tuple(result)


def parse_manual_languages(values: list[str]) -> tuple[Language, ...]:
    """Parse repeatable ``NAME=CEFR`` CLI values."""
    if not values or any("=" not in value for value in values):
        raise ValueError("язык задаётся как NAME=A1|A2|B1|B2|C1|C2")
    result: list[Language] = []
    seen: set[str] = set()
    for value in values:
        name, level = value.rsplit("=", 1)
        name = _normalized_name(name)
        level = level.strip().upper()
        if not name or level not in CEFR_LEVELS:
            raise ValueError("язык задаётся как NAME=A1|A2|B1|B2|C1|C2")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"дублирующийся язык: {name}")
        seen.add(key)
        result.append(Language(name, level))
    return tuple(result)


def build_languages_prompt(
    page_text: str, existing: tuple[str, ...], mode: str
) -> list[dict[str, str]]:
    task = "с нуля" if mode == "fresh" else "до-заполнения, сохраняя существующие"
    system = (
        "Верни только JSON-массив объектов с ровно полями name и level. "
        "Извлекай только явно указанные языки. Поле level ВСЕГДА должно быть null: "
        "не угадывай CEFR, его подтвердит пользователь отдельно."
    )
    user = (
        f"Режим: {task}. Уже есть: {json.dumps(existing, ensure_ascii=False)}.\n"
        f"Текст резюме:\n{page_text[:12000]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def edit_languages_on_hh(
    page, resume: ResumeConfig, languages: tuple[Language, ...], *, dry_run: bool, mode: str
) -> LanguagesResult:
    """Apply confirmed language rows through the live modal, or plan only."""
    if dry_run:
        return LanguagesResult(True, languages)
    try:
        goto_hh(page, f"{HH_BASE_URL}/resume/{resume.resume_id}")
        if not has_auth_cookie(page) or has_login_form(page):
            return LanguagesResult(False, languages, "сессия hh.ru не подтверждена")
        if urlsplit(page.url).path != f"/resume/{resume.resume_id}":
            return LanguagesResult(False, languages, "страница нужного резюме не подтверждена")
        card = page.locator(resume_page.RESUME_LANGUAGE_CARD)
        edit = card.locator(resume_page.RESUME_LANGUAGE_EDIT_BUTTON)
        if card.count() != 1 or edit.count() != 1:
            return LanguagesResult(False, languages, "карточка языков не найдена однозначно")
        existing = tuple(
            tag.inner_text().split(",", 1)[0].strip()
            for tag in card.locator(resume_page.RESUME_LANGUAGE_TAG).all()
        )
        existing_keys = {value.casefold() for value in existing}
        if mode == "fresh" and existing:
            return LanguagesResult(False, languages, "режим с нуля требует пустого раздела")
        additions = tuple(item for item in languages if item.name.casefold() not in existing_keys)
        edit.click()
        dialog = page.get_by_role("dialog", name="Язык").last()
        dialog.wait_for(state="visible")
        for item in additions:
            if item.level is None:
                return LanguagesResult(
                    False,
                    languages,
                    f"уровень CEFR для языка '{item.name}' не подтверждён",
                )
            add = page.locator(resume_page.RESUME_LANGUAGE_ADD_BUTTON)
            if add.count() != 1:
                return LanguagesResult(
                    False, languages, "кнопка добавления языка не найдена однозначно"
                )
            add.click()
            form = page.locator(resume_page.RESUME_LANGUAGE_ADD_FORM)
            form.wait_for(state="visible")
            _choose_language(page, form, item.name)
            _choose_degree(page, form, item.level)
            _save_language(page)
            dialog = page.get_by_role("dialog", name="Язык").last()
            dialog.wait_for(state="hidden")
        return LanguagesResult(True, languages, acted=bool(additions))
    except (PlaywrightError, RuntimeError) as exc:
        return LanguagesResult(
            False, languages, f"сохранение языка не подтверждено: {exc}", acted=True
        )


def _choose_language(page, form, name: str) -> None:
    selectors = form.locator(resume_page.RESUME_LANGUAGE_SELECT)
    if selectors.count() != 2:
        raise PlaywrightError("поля языка/уровня не найдены однозначно")
    selectors.nth(0).click()
    picker = page.get_by_role("dialog", name="Язык").last()
    picker.get_by_role("option", name=name, exact=True).click()


def _choose_degree(page, form, level: str) -> None:
    selectors = form.locator(resume_page.RESUME_LANGUAGE_SELECT)
    if selectors.count() != 2:
        raise PlaywrightError("поля языка/уровня не найдены однозначно")
    selectors.nth(1).click()
    picker = page.get_by_role("dialog", name="Уровень владения").last()
    picker.get_by_role("option", name=CEFR_LABELS[level], exact=True).click()


def _save_language(page) -> None:
    dialog = page.get_by_role("dialog", name="Язык").last()
    save = dialog.locator(resume_page.RESUME_LANGUAGE_SAVE)
    if save.count() != 1:
        raise PlaywrightError("кнопка сохранения языка не найдена однозначно")
    save.click()

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
    """Parse strict JSON from the LLM; reject any non-null level outright.

    Unlike ``parse_manual_languages``, this parser never accepts a concrete
    CEFR value — the LLM is instructed to always return ``level: null``
    (``build_languages_prompt``), and a model that ignores that instruction
    (a slip, or a prompt injection from the resume text) must not be able to
    smuggle a guessed level into a write. A non-null ``level`` is therefore
    always a parse error, even if it happens to be a syntactically valid
    CEFR code — accepting "valid-looking" values here is exactly the gap
    that let a guessed level reach ``edit_languages_on_hh`` unconfirmed.
    """
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
        if level is not None:
            raise ValueError("LLM не должен указывать уровень CEFR; поле level должно быть null")
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
    """Apply confirmed language rows through the live modal, or plan only.

    Languages are a profile-level entity on hh.ru, not a resume-level one:
    ``/resume/{id}`` never renders a languages block (confirmed on an empty
    draft and on a published resume with real language data).  The editor
    lives on ``/applicant/profile/me`` and a saved language applies to every
    resume on the account.  ``resume`` is accepted for interface symmetry
    with the other ``edit_*_on_hh`` functions and for the ``resume.id`` used
    in the caller's logging/confirmation prompts — the write itself does not
    read any resume-specific field from it, since ``page`` already carries
    the account session and there is no per-resume target to select.
    """
    if dry_run:
        return LanguagesResult(True, languages)
    try:
        goto_hh(page, f"{HH_BASE_URL}/applicant/profile/me")
        if not has_auth_cookie(page) or has_login_form(page):
            return LanguagesResult(False, languages, "сессия hh.ru не подтверждена")
        if urlsplit(page.url).path != "/applicant/profile/me":
            return LanguagesResult(False, languages, "страница профиля не подтверждена")
        card = page.locator(resume_page.RESUME_LANGUAGE_CARD)
        add_button = page.locator(resume_page.RESUME_LANGUAGE_ADD_BUTTON)
        # #265 code-review round 1: an immediate count() right after goto_hh can
        # observe the DOM before the profile SPA finishes hydrating (the same
        # commit-vs-render race documented in CLAUDE.md for resume_position.py /
        # skills.py / delete_resume.py). Wait for the card to render before the
        # strict count check; a genuinely missing card times out here and still
        # fails closed with our own message, not a generic PlaywrightError one.
        try:
            card.first.wait_for(state="visible", timeout=15000)
        except PlaywrightError:
            pass
        if card.count() != 1 or add_button.count() != 1:
            return LanguagesResult(False, languages, "карточка языков не найдена однозначно")
        existing = tuple(
            row.locator(resume_page.RESUME_LANGUAGE_ROW_CELL_TEXT).first.inner_text().strip()
            for row in card.locator(resume_page.RESUME_LANGUAGE_ROW).all()
        )
        existing_keys = {value.casefold() for value in existing}
        if mode == "fresh" and existing:
            return LanguagesResult(False, languages, "режим с нуля требует пустого раздела")
        additions = tuple(item for item in languages if item.name.casefold() not in existing_keys)
        for item in additions:
            if item.level is None:
                return LanguagesResult(
                    False,
                    languages,
                    f"уровень CEFR для языка '{item.name}' не подтверждён",
                )
            add_button.click()
            dialog = page.get_by_role("dialog", name="Язык").last
            dialog.wait_for(state="visible")
            form = dialog.locator(resume_page.RESUME_LANGUAGE_ADD_FORM)
            form.wait_for(state="visible")
            _choose_language(page, form, item.name)
            _choose_degree(page, form, item.level)
            _save_language(dialog)
            dialog.wait_for(state="hidden")
        return LanguagesResult(True, languages, acted=bool(additions))
    except (PlaywrightError, RuntimeError) as exc:
        return LanguagesResult(
            False, languages, f"сохранение языка не подтверждено: {exc}", acted=True
        )


def _choose_language(page, form, name: str) -> None:
    selector = form.locator(resume_page.RESUME_LANGUAGE_FORM_LANGUAGE_SELECT)
    if selector.count() != 1:
        raise PlaywrightError("поле выбора языка не найдено однозначно")
    selector.click()
    dialog = page.get_by_role("dialog", name="Язык").last
    dialog.get_by_role("option", name=name, exact=True).click()


def _choose_degree(page, form, level: str) -> None:
    selector = form.locator(resume_page.RESUME_LANGUAGE_FORM_DEGREE_SELECT)
    if selector.count() != 1:
        raise PlaywrightError("поле выбора уровня не найдено однозначно")
    selector.click()
    dialog = page.get_by_role("dialog", name="Язык").last
    option = dialog.locator(resume_page.RESUME_LANGUAGE_DEGREE_OPTION.format(level.lower()))
    if option.count() != 1:
        raise PlaywrightError(f"опция уровня '{level}' не найдена однозначно")
    option.click()


def _save_language(dialog) -> None:
    save = dialog.locator(resume_page.RESUME_LANGUAGE_SAVE)
    if save.count() != 1:
        raise PlaywrightError("кнопка сохранения языка не найдена однозначно")
    save.click()

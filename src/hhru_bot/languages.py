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

from .browser import HH_BASE_URL, LOGIN_FORM, goto_hh, has_auth_cookie, has_login_form
from .config import ResumeConfig
from .selector_groups import resume_page

CEFR_LEVELS = frozenset(("A1", "A2", "B1", "B2", "C1", "C2"))
_LANGUAGE_PROFILE_PATH = "/applicant/profile/me"
_LANGUAGE_PROFILE_READY_SELECTOR = f"{resume_page.RESUME_LANGUAGE_CARD}:visible, {LOGIN_FORM}"


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


def wait_for_language_card(page):
    """Wait past the profile SPA hydration race and return the strictly-one
    languages card locator, or ``None`` if it isn't unambiguous.

    #265 code-review round 2 (/review): this "wait for card, then require
    count()==1, else fail closed" logic was duplicated verbatim between
    commands/edit_languages.py (reading ``existing`` for the LLM prompt) and
    edit_languages_on_hh (the write path) — a selector or race-condition fix
    applied to one would silently not apply to the other. Both callers now
    share this helper.
    """
    card = page.locator(resume_page.RESUME_LANGUAGE_CARD)
    try:
        card.first.wait_for(state="visible", timeout=15000)
    except PlaywrightError:
        return None
    return card if card.count() == 1 else None


def _open_language_profile(page):
    """Open the shared language profile after its hydrated marker appears.

    React #418/#423 is diagnostic: hh.ru may recover by client-rendering the
    profile.  Readiness is therefore proved by the language card, not by a
    clean console.  The login form is included in the navigation marker so an
    expired session can be rejected immediately instead of waiting through
    every hydration retry.
    """
    goto_hh(
        page,
        f"{HH_BASE_URL}{_LANGUAGE_PROFILE_PATH}",
        ready_selector=_LANGUAGE_PROFILE_READY_SELECTOR,
    )
    if not has_auth_cookie(page) or has_login_form(page):
        raise RuntimeError("сессия hh.ru не подтверждена")
    if urlsplit(page.url).path != _LANGUAGE_PROFILE_PATH:
        raise RuntimeError("страница профиля не подтверждена")
    card = wait_for_language_card(page)
    if card is None:
        raise RuntimeError("карточка языков не найдена однозначно")
    return card


def read_existing_languages(card) -> tuple[str, ...]:
    """Read the language names already on the (already-confirmed) card."""
    return tuple(
        row.locator(resume_page.RESUME_LANGUAGE_ROW_CELL_TEXT).first.inner_text().strip()
        for row in card.locator(resume_page.RESUME_LANGUAGE_ROW).all()
    )


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
    acted = False
    try:
        card = _open_language_profile(page)
        add_button = page.locator(resume_page.RESUME_LANGUAGE_ADD_BUTTON)
        if add_button.count() != 1:
            return LanguagesResult(
                False, languages, "кнопка добавления языка не найдена однозначно"
            )
        existing = read_existing_languages(card)
        existing_keys = {value.casefold() for value in existing}
        if mode == "fresh" and existing:
            return LanguagesResult(False, languages, "режим с нуля требует пустого раздела")
        additions = tuple(item for item in languages if item.name.casefold() not in existing_keys)
        # #265 code-review round 2 (Codex/claude): the level==None check must
        # run before ANY click, not inside the write loop. A guard inside the
        # loop lets earlier additions in the same call already be clicked and
        # saved on hh.ru before a later item without a confirmed level aborts
        # the whole call with success=False (partial-write, live side effects
        # hidden behind an apparent failure) — and since parse_language_plan
        # now guarantees every LLM-sourced Language has level=None (round 1
        # fix), this same in-loop guard made the LLM path fail on its very
        # first addition, so the advertised LLM-fill workflow could never
        # write anything. Failing closed here, before the loop starts, keeps
        # the call a true no-op (acted=False) and gives a caller the full
        # list of languages still needing a confirmed level, instead of
        # aborting after already writing some of them.
        unconfirmed = tuple(item.name for item in additions if item.level is None)
        if unconfirmed:
            return LanguagesResult(
                False,
                languages,
                "уровень CEFR не подтверждён для: " + ", ".join(unconfirmed),
            )
        for item in additions:
            # unconfirmed (above) already proved every item.level is non-None.
            assert item.level is not None
            add_button.click()
            dialog = page.get_by_role("dialog", name="Язык").last
            dialog.wait_for(state="visible")
            form = dialog.locator(resume_page.RESUME_LANGUAGE_ADD_FORM)
            form.wait_for(state="visible")
            _choose_language(page, form, item.name)
            _choose_degree(page, form, item.level)
            save = _language_save_button(dialog)
            # The save click is the first operation that can persist data on
            # hh.ru.  Mark it before clicking because Playwright may throw
            # after the request has already left the browser.
            acted = True
            save.click()
            dialog.wait_for(state="hidden")
            # #265 code-review round 2 (Codex): the dialog closing is not proof
            # the write persisted — a rerender, an ambiguous server response,
            # or an optimistic modal close can hide the dialog without the row
            # actually landing. Re-read the card and require the language name
            # to now be present (RESUME_LANGUAGE_ROW_CELL_TEXT is confirmed;
            # the CEFR level's position within a row is not, so this check
            # only confirms the name, not the level — see resume_page.py).
            confirmed_names = {name.casefold() for name in read_existing_languages(card)}
            if item.name.casefold() not in confirmed_names:
                return LanguagesResult(
                    False,
                    languages,
                    f"сохранение языка '{item.name}' не подтверждено после закрытия диалога",
                    acted=True,
                )
        return LanguagesResult(True, languages, acted=bool(additions))
    except (PlaywrightError, RuntimeError) as exc:
        return LanguagesResult(
            False, languages, f"сохранение языка не подтверждено: {exc}", acted=acted
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


def _language_save_button(dialog):
    """Return the single confirmed save control without clicking it."""
    save = dialog.locator(resume_page.RESUME_LANGUAGE_SAVE)
    if save.count() != 1:
        raise PlaywrightError("кнопка сохранения языка не найдена однозначно")
    return save

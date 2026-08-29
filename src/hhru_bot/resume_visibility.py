"""Browser step for changing resume visibility and the employer stop-list.

Экран `/resume/edit/{resume_id}/visibility` подтверждён живым DOM 2026-08-29
(issue #746; предыдущая fail-closed заглушка была от issue #566). Действие
только UI-кликами, без ``page.request.*`` — как весь проект.

Пять режимов видимости (CLAUDE.md/docs/cli-spec.md, #566):
``everyone``/``no-one``/``link-only`` не имеют списка компаний.
``whitelist``/``blacklist`` открывают блок "Кто видит"/"Кто не видит" со
своим списком работодателей — команда может редактировать этот список
независимо от того, меняется ли режим в этом же вызове (issue #746: стоп-лист
обычно общий для всех резюме аккаунта, а смена режима — отдельное решение).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import HH_BASE_URL, goto_hh
from .external_forms.detect import normalize
from .selector_groups.resume_visibility import (
    RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DATA_QA_PREFIX,
    RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DELETE,
    RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_CHECKBOX,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX,
    RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_NAME,
    RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST,
    RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_WHITELIST,
    RESUME_VISIBILITY_MODAL_CLOSE,
    RESUME_VISIBILITY_MODAL_CONFIRM,
    RESUME_VISIBILITY_MODE_BLACKLIST,
    RESUME_VISIBILITY_MODE_EVERYONE,
    RESUME_VISIBILITY_MODE_LINK_ONLY,
    RESUME_VISIBILITY_MODE_NO_ONE,
    RESUME_VISIBILITY_MODE_WHITELIST,
    RESUME_VISIBILITY_SAVE,
)

# Каноническая пятёрка режимов (docs/cli-spec.md §resume-visibility, #566).
# Значения — то, что принимает --mode; ключи, оставшиеся из issue #746
# ("public"/"selected"/"hidden-from"/"hidden") сюда сознательно не взяты —
# сигнатура #566 уже задокументирована и слита в main, дублировать её другим
# словарём означало бы два конкурирующих контракта для одной команды.
VISIBILITY_MODES = ("everyone", "no-one", "link-only", "whitelist", "blacklist")

_MODE_SELECTORS: dict[str, str] = {
    "everyone": RESUME_VISIBILITY_MODE_EVERYONE,
    "whitelist": RESUME_VISIBILITY_MODE_WHITELIST,
    "blacklist": RESUME_VISIBILITY_MODE_BLACKLIST,
    "link-only": RESUME_VISIBILITY_MODE_LINK_ONLY,
    "no-one": RESUME_VISIBILITY_MODE_NO_ONE,
}
# Только whitelist/blacklist рендерят блок со списком компаний.
_EMPLOYER_LIST_MODES = ("whitelist", "blacklist")
_ACTIVATOR_SELECTORS: dict[str, str] = {
    "whitelist": RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_WHITELIST,
    "blacklist": RESUME_VISIBILITY_EMPLOYERS_ACTIVATOR_BLACKLIST,
}


def visibility_url(resume_id: str) -> str:
    return f"{HH_BASE_URL}/resume/edit/{resume_id}/visibility"


@dataclass
class EmployerCandidate:
    employer_id: str
    name: str
    city: str = ""


@dataclass
class ResumeVisibilityResult:
    resume_id: str
    success: bool
    reason: str
    uncertain: bool = False
    # Неоднозначные найденные компании (несколько карточек на одно имя) —
    # разрешение остаётся за пользователем (issue #746), команда не выбирает
    # автоматически. Заполняется, когда success=False именно по этой причине.
    ambiguous_candidates: list[EmployerCandidate] = field(default_factory=list)
    ambiguous_query: str = ""


def _one(page: Page, selector: str, label: str) -> tuple[Locator | None, str]:
    locator = page.locator(selector)
    count = locator.count()
    if count != 1:
        return None, f"{label} не подтверждён однозначно (совпадений: {count})"
    return locator.first, ""


def _click_mode(page: Page, mode: str) -> str:
    """Click the outer access-type label; inputs share value='on'/blank name."""
    locator, reason = _one(page, _MODE_SELECTORS[mode], f"режим видимости «{mode}»")
    if reason:
        return reason
    assert locator is not None
    locator.click()
    return ""


def _read_employer_search_results(page: Page) -> list[EmployerCandidate]:
    items = page.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX)
    candidates: list[EmployerCandidate] = []
    for item in items.all():
        qa = item.get_attribute("data-qa") or ""
        employer_id = qa.removeprefix(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX)
        name_locator = item.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_NAME)
        name = name_locator.first.text_content() if name_locator.count() else ""
        candidates.append(EmployerCandidate(employer_id=employer_id, name=(name or "").strip()))
    return candidates


def _open_employer_list_modal(page: Page, list_mode: str) -> str:
    activator, reason = _one(
        page, _ACTIVATOR_SELECTORS[list_mode], f"блок списка работодателей «{list_mode}»"
    )
    if reason:
        return reason
    assert activator is not None
    activator.click()
    try:
        page.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT).first.wait_for(
            state="visible", timeout=15000
        )
    except PlaywrightError as exc:
        return f"модалка списка работодателей не отрисовалась: {exc}"
    return ""


def _add_employer(page: Page, name: str) -> tuple[bool, str, list[EmployerCandidate]]:
    """Search, resolve exactly one candidate, check it, and click Добавить.

    Ambiguous/zero matches are a fail-closed refusal — issue #746 requires
    resolving multiple similarly-named employers to stay with the caller
    (interactively confirmed one level up), never an automatic pick.
    """
    search, reason = _one(page, RESUME_VISIBILITY_EMPLOYER_SEARCH_INPUT, "поиск работодателя")
    if reason:
        return False, reason, []
    assert search is not None
    search.fill(name)
    try:
        page.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_PREFIX).first.wait_for(
            state="visible", timeout=15000
        )
    except PlaywrightError:
        return False, f"работодатель «{name}» не найден в поиске hh.ru", []
    candidates = _read_employer_search_results(page)
    matches = [c for c in candidates if normalize(c.name) == normalize(name)]
    if not matches:
        return False, f"работодатель «{name}» не найден в поиске hh.ru (точное совпадение)", []
    if len(matches) > 1:
        return False, f"найдено {len(matches)} работодателей с именем «{name}» — уточните", matches
    target = matches[0]
    item_selector = (
        f"[data-qa='{RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_ITEM_DATA_QA_PREFIX}"
        f"{target.employer_id}']"
    )
    row, reason = _one(page, item_selector, f"карточка работодателя «{name}»")
    if reason:
        return False, reason, []
    assert row is not None
    checkbox = row.locator(RESUME_VISIBILITY_EMPLOYER_SEARCH_RESULT_CHECKBOX)
    if checkbox.count() != 1:
        return False, f"чекбокс работодателя «{name}» не подтверждён однозначно", []
    checkbox.first.check()
    confirm, reason = _one(page, RESUME_VISIBILITY_MODAL_CONFIRM, "кнопка «Добавить»")
    if reason:
        return False, reason, []
    assert confirm is not None
    confirm.click()
    return True, "", []


def _remove_employer(page: Page, name: str) -> tuple[bool, str]:
    """Remove an already-added employer by exact name match from the list view."""
    items = page.locator(RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_PREFIX)
    matches: list[Locator] = []
    for item in items.all():
        qa = item.get_attribute("data-qa") or ""
        if not qa.startswith(RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DATA_QA_PREFIX):
            continue
        if normalize(item.text_content() or "").find(normalize(name)) == -1:
            continue
        matches.append(item)
    if not matches:
        return False, f"работодатель «{name}» не найден в текущем списке"
    if len(matches) > 1:
        return False, f"в списке {len(matches)} записей, содержащих «{name}» — уточните точное имя"
    delete_button = matches[0].locator(RESUME_VISIBILITY_EMPLOYER_LIST_ITEM_DELETE)
    if delete_button.count() != 1:
        return False, f"кнопка удаления работодателя «{name}» не подтверждена однозначно"
    delete_button.first.click()
    return True, ""


def set_resume_visibility_on_hh(
    page: Page,
    resume,  # noqa: ANN001 - kept compatible with the Playwright command seam
    mode: str | None,
    dry_run: bool,
    *,
    add_employers: tuple[str, ...] = (),
    remove_employers: tuple[str, ...] = (),
    before_click: Callable[[], None] | None = None,
) -> ResumeVisibilityResult:
    """Change the visibility mode and/or the employer stop-list for one resume.

    ``mode=None`` keeps the resume's current mode untouched — only the
    employer list is edited (issue #746's primary scenario: a stop-list
    entry usually applies without a mode change). ``add_employers``/
    ``remove_employers`` require an active whitelist/blacklist mode (either
    passed via ``mode`` or already selected on hh.ru) — editing a list that
    is not the active one would silently not apply.
    """
    resume_id = resume.resume_id
    if mode is not None and mode not in VISIBILITY_MODES:
        return ResumeVisibilityResult(resume_id, False, f"неизвестный режим видимости «{mode}»")
    if dry_run:
        parts = []
        if mode is not None:
            parts.append(f"режим будет изменён на «{mode}»")
        for name in add_employers:
            parts.append(f"будет добавлен работодатель «{name}»")
        for name in remove_employers:
            parts.append(f"будет удалён работодатель «{name}»")
        if not parts:
            return ResumeVisibilityResult(
                resume_id, False, "не задано ни --mode, ни списки работодателей"
            )
        return ResumeVisibilityResult(resume_id, True, "dry-run; " + "; ".join(parts))

    goto_hh(page, visibility_url(resume_id))
    try:
        page.locator(RESUME_VISIBILITY_SAVE).first.wait_for(state="visible", timeout=15000)
    except PlaywrightError as exc:
        return ResumeVisibilityResult(resume_id, False, f"экран видимости не отрисовался: {exc}")

    if mode is not None:
        reason = _click_mode(page, mode)
        if reason:
            return ResumeVisibilityResult(resume_id, False, reason)

    wants_employer_edit = bool(add_employers or remove_employers)
    if wants_employer_edit:
        # Determine the active list mode: explicit --mode wins; otherwise read
        # which whitelist/blacklist radio is currently checked on hh.ru.
        list_mode = mode if mode in _EMPLOYER_LIST_MODES else None
        if list_mode is None:
            for candidate in _EMPLOYER_LIST_MODES:
                sel = _MODE_SELECTORS[candidate]
                loc = page.locator(sel).locator("input[type='radio']")
                if loc.count() == 1 and loc.first.is_checked():
                    list_mode = candidate
                    break
        if list_mode is None:
            return ResumeVisibilityResult(
                resume_id,
                False,
                "список работодателей задан, но активный режим не whitelist/blacklist "
                "(укажите --mode whitelist или --mode blacklist)",
            )
        reason = _open_employer_list_modal(page, list_mode)
        if reason:
            return ResumeVisibilityResult(resume_id, False, reason)

        for name in add_employers:
            ok, reason, ambiguous = _add_employer(page, name)
            if not ok:
                return ResumeVisibilityResult(
                    resume_id,
                    False,
                    reason,
                    ambiguous_candidates=ambiguous,
                    ambiguous_query=name if ambiguous else "",
                )
        for name in remove_employers:
            ok, reason = _remove_employer(page, name)
            if not ok:
                return ResumeVisibilityResult(resume_id, False, reason)

        close, reason = _one(page, RESUME_VISIBILITY_MODAL_CLOSE, "кнопка закрытия модалки списка")
        if reason:
            return ResumeVisibilityResult(resume_id, False, reason)
        assert close is not None
        close.click()

    save, reason = _one(page, RESUME_VISIBILITY_SAVE, "кнопка «Сохранить»")
    if reason:
        return ResumeVisibilityResult(resume_id, False, reason)
    assert save is not None
    try:
        if before_click is not None:
            before_click()
        save.click()
        # hh.ru пересобирает форму после сохранения; ждём исчезновения кнопки
        # или редиректа как позитивного сигнала вместо фиксированного sleep.
        page.locator(RESUME_VISIBILITY_SAVE).first.wait_for(state="hidden", timeout=15000)
    except PlaywrightError as exc:
        return ResumeVisibilityResult(
            resume_id,
            False,
            f"ошибка после клика «Сохранить»: {exc}",
            uncertain=True,
        )
    return ResumeVisibilityResult(resume_id, True, "видимость сохранена")

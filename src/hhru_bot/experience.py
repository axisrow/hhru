"""LLM planning and UI editing of resume experience entries (#261).

The LLM only proposes structured text.  This module never uses hh.ru HTTP
endpoints; writes are UI clicks and the save button is never touched in
``dry_run`` mode.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    HH_BASE_URL,
    NotAuthenticated,
    goto_hh,
    open_confirmed_resume,
    require_authenticated_page,
    resume_identity_matches,
)
from .selector_groups.resume_experience import (
    EXPERIENCE_CANCEL,
    EXPERIENCE_COMPANY,
    EXPERIENCE_COMPANY_URL,
    EXPERIENCE_DESCRIPTION,
    EXPERIENCE_EDIT_BUTTON,
    EXPERIENCE_EDIT_BUTTONS_ALL,
    EXPERIENCE_END_MONTH,
    EXPERIENCE_END_YEAR,
    EXPERIENCE_EXPAND_BUTTON,
    EXPERIENCE_MONTH_LISTBOX,
    EXPERIENCE_MONTH_OPTION,
    EXPERIENCE_POSITION,
    EXPERIENCE_SAVE,
    EXPERIENCE_START_MONTH,
    EXPERIENCE_START_YEAR,
    FIRST_EXPERIENCE_CANCEL,
    FIRST_EXPERIENCE_COMPANY,
    FIRST_EXPERIENCE_CURRENT_CHECKBOX,
    FIRST_EXPERIENCE_POSITION,
    FIRST_EXPERIENCE_SAVE,
)

logger = logging.getLogger("hhru_bot.experience")
SAVE_TIMEOUT_MS = 30_000
FORM_TIMEOUT_MS = 10_000
# #811: the month combobox popup is a React-rendered dialog opened by a click
# on the trigger — CLAUDE.md pattern requires an explicit wait_for(visible)
# after that click, not just count()==1, or a slow render reads as "option
# not found" (see EXPERIENCE_MONTH_OPTION provenance comment for the
# live-confirmed shape).
MONTH_OPTION_TIMEOUT_MS = 5_000

# Confirmed live 2026-08-30 (draft resume): each combobox's 12 options carry
# these exact labels in this order — Январь=01 .. Декабрь=12. Used both to
# build the click target (_select_month) and to parse the trigger's
# inner_text() back into a "1".."12" string when reading (the trigger is not
# an <input>/<select>, input_value() raises on it — confirmed live).
MONTH_NAMES = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)


@dataclass
class ExperienceEntry:
    company: str = ""
    position: str = ""
    start_year: str = ""
    start_month: str = ""
    end_year: str = ""
    end_month: str = ""
    current: bool = False
    duties: str = ""
    achievements: list[str] | str = ""
    company_url: str = ""

    def description(self) -> str:
        achievements = self.achievements
        if isinstance(achievements, str):
            achievements_text = achievements.strip()
        else:
            achievements_text = "\n".join(
                f"- {item.strip()}" for item in achievements if item.strip()
            )
        duties = self.duties.strip()
        if duties and achievements_text:
            return f"{duties}\n\nДостижения:\n{achievements_text}"
        return duties or achievements_text


@dataclass
class ExperiencePlan:
    entries: list[ExperienceEntry]
    used_fallback: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ExperienceResult:
    """Structural outcome for one experience row."""

    reason: str
    success: bool = False
    uncertain: bool = False


def _entry(raw: Any) -> ExperienceEntry | None:
    if not isinstance(raw, dict):
        return None
    achievements = raw.get("achievements", "")
    if not isinstance(achievements, (str, list)) or (
        isinstance(achievements, list) and not all(isinstance(v, str) for v in achievements)
    ):
        return None
    values = {
        key: raw.get(key, "")
        for key in (
            "company",
            "position",
            "start_year",
            "start_month",
            "end_year",
            "end_month",
            "duties",
            "company_url",
        )
    }
    if not all(isinstance(v, str) for v in values.values()):
        return None
    current = raw.get("current", False)
    if not isinstance(current, bool):
        return None
    return ExperienceEntry(**values, current=current, achievements=achievements)


def parse_plan(content: str | None) -> list[ExperienceEntry] | None:
    """Parse a strict JSON array returned by the model; invalid output is unusable."""
    if not content or not content.strip():
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list) or not raw:
        return None
    result = [_entry(item) for item in raw]
    if any(item is None for item in result):
        return None
    return cast("list[ExperienceEntry]", result)


def build_prompt(mode: str, career: str, existing: list[ExperienceEntry] | None = None):
    """Build a fact-preserving prompt.  ``existing`` is the do-fill context."""
    system = (
        "Ты помогаешь заполнить опыт работы в резюме. Отвечай только JSON-массивом "
        "объектов с полями company, position, start_year, start_month, end_year, "
        "end_month, current, duties, achievements, company_url. Не выдумывай факты, "
        "даты, метрики или URL: используй только сведения пользователя. "
        'start_month/end_month — число месяца от 1 до 12 строкой (например, "3"); '
        "start_month обязателен, форма резюме hh.ru не сохранится без него. "
        "achievements — список строк. current=true означает работу по настоящее время "
        "(end_year/end_month в этом случае оставь пустыми)."
    )
    context: dict[str, Any] = {"career": career, "mode": mode}
    if existing is not None:
        context["existing"] = [entry.__dict__ for entry in existing]
    instruction = (
        "Сгенерируй записи с нуля по описанию карьеры."
        if mode == "create"
        else (
            "Допиши только пустые обязанности и достижения, сохрани остальные "
            "значения существующих записей."
        )
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{instruction}\n{json.dumps(context, ensure_ascii=False)}"},
    ]


def plan_experience(llm_client, *, mode: str, career: str, existing=None) -> ExperiencePlan:
    """Call the shared LLM client.  Any AI failure falls back without inventing data."""
    if mode not in ("create", "fill"):
        raise ValueError("mode must be 'create' or 'fill'")
    try:
        response = llm_client.chat(build_prompt(mode, career, existing), temperature=0.3)
        entries = parse_plan(response.content if response is not None else None)
    except Exception as exc:  # noqa: BLE001 - AI fallback is deliberately broad
        logger.warning("Experience generation failed: %s", exc)
        entries = None
        reason = f"LLM недоступен: {exc}"
    else:
        reason = "LLM вернул неполный или невалидный JSON"
    if entries is not None and mode == "fill" and existing is not None:
        entries = _merge_fill_plan(existing, entries)
        if entries is None:
            return ExperiencePlan(
                list(existing),
                used_fallback=True,
                reason="LLM изменил защищённые поля или число записей",
            )
    if entries is None:
        # In fill mode preserving existing data is safe.  In create mode an empty
        # plan is safer than writing fabricated content or a guessed fallback.
        return ExperiencePlan(list(existing or []), used_fallback=True, reason=reason)
    # #811 review: build_prompt() asks the LLM for start_month, but nothing
    # upstream enforces it — parse_plan()/_entry() accept an empty string like
    # any other optional field, and _merge_fill_plan only protects start_month
    # from being *changed*, not from being blank on both sides (e.g. existing
    # rows read from hh.ru before this fix). Without this check a plan with a
    # real company/position but blank start_month reaches edit_experience_on_hh
    # and only fails once the hh.ru form itself rejects the save — same failure
    # mode the CLI --entry path already prevents via _load_entries. Skip rows
    # with no identity at all (LLM legitimately proposing nothing to fill).
    missing_month = [
        entry
        for entry in entries
        if (entry.company.strip() or entry.position.strip()) and not entry.start_month.strip()
    ]
    if missing_month:
        return ExperiencePlan(
            list(existing or []),
            used_fallback=True,
            reason=(
                "LLM не указал start_month для "
                f"{len(missing_month)} записи(ей) — форма опыта hh.ru не "
                "сохраняется без месяца начала работы (#811)"
            ),
        )
    return ExperiencePlan(entries)


def _merge_fill_plan(
    existing: list[ExperienceEntry], proposed: list[ExperienceEntry]
) -> list[ExperienceEntry] | None:
    """Keep identity fields and existing text authoritative in ``fill`` mode."""
    if len(existing) != len(proposed):
        return None
    merged = []
    for old, new in zip(existing, proposed, strict=True):
        protected = (
            "company",
            "position",
            "start_year",
            "start_month",
            "end_year",
            "end_month",
            "current",
            "company_url",
        )
        if any(getattr(old, key) != getattr(new, key) for key in protected):
            return None
        merged.append(
            ExperienceEntry(
                company=old.company,
                position=old.position,
                start_year=old.start_year,
                start_month=old.start_month,
                end_year=old.end_year,
                end_month=old.end_month,
                current=old.current,
                company_url=old.company_url,
                duties=old.duties or new.duties,
                achievements=old.achievements or new.achievements,
            )
        )
    return merged


def _fill(locator, value: str) -> None:
    if locator.count() != 1:
        raise ValueError(f"поле определяется неоднозначно ({locator.count()})")
    locator.fill(value)


def _read(locator) -> str:
    if locator.count() != 1:
        raise ValueError(f"поле определяется неоднозначно ({locator.count()})")
    return locator.input_value()


def _read_month(locator) -> str:
    """Read a month combobox's current selection as "1".."12", "" if unset.

    The trigger is a ``role="combobox"`` <div>, not an <input>/<select> —
    ``input_value()`` raises on it (confirmed live 2026-08-30). The rendered
    label is "Месяц" when nothing is chosen, "Месяц\\n<Название>" once a
    month is picked (confirmed live) — parse the second line against
    MONTH_NAMES rather than trusting an untyped free-text match.
    """
    if locator.count() != 1:
        raise ValueError(f"поле определяется неоднозначно ({locator.count()})")
    lines = locator.inner_text().splitlines()
    if len(lines) < 2:
        return ""
    label = lines[1].strip()
    try:
        return str(MONTH_NAMES.index(label) + 1)
    except ValueError:
        return ""


def _select_month(page: Page, locator, month: str) -> None:
    """Open a month combobox and click the option matching ``month`` ("1".."12").

    Mirrors apply/steps.py::_select_resume_in_form: identity-bound click via
    EXPERIENCE_MONTH_OPTION (data-qa already carries the 2-digit month), fail
    on ambiguity rather than falling back to a guess. The click opens a
    React-rendered popup (CLAUDE.md pattern) — wait_for(state="visible") on
    the option before asserting count()==1, then wait for the listbox itself
    to close (confirmed live: count drops to 0 after a normal option click,
    no extra toggle-close step needed, unlike the resume dropdown in apply).
    """
    if locator.count() != 1:
        raise ValueError(f"поле месяца определяется неоднозначно ({locator.count()})")
    try:
        month_number = int(month)
    except ValueError as exc:
        raise ValueError(f"месяц должен быть числом 1-12: {month!r}") from exc
    if not 1 <= month_number <= 12:
        raise ValueError(f"месяц должен быть числом 1-12: {month!r}")
    locator.click()
    option = page.locator(EXPERIENCE_MONTH_OPTION.format(month=f"{month_number:02d}"))
    option.wait_for(state="visible", timeout=MONTH_OPTION_TIMEOUT_MS)
    if option.count() != 1:
        raise ValueError(f"опция месяца {month_number:02d} определяется неоднозначно")
    option.click()
    page.locator(EXPERIENCE_MONTH_LISTBOX).wait_for(state="hidden", timeout=MONTH_OPTION_TIMEOUT_MS)


ROW_HYDRATION_TIMEOUT_MS = 5_000


def _expand_experience_list(page: Page) -> None:
    """Click "Развернуть" if hh.ru collapsed the experience list (#815).

    Confirmed live 2026-08-30: a resume with more than 3 experience entries
    renders only 3 edit-trigger buttons until this control is clicked — the
    remaining rows are not in the DOM at all, not just visually hidden, so
    no amount of waiting on the existing buttons would reveal them.  Absent
    on resumes with 3 or fewer entries (including empty drafts), so this is
    a no-op there.
    """
    expand = page.locator(EXPERIENCE_EXPAND_BUTTON)
    if expand.count() == 1:
        try:
            expand.wait_for(state="visible", timeout=ROW_HYDRATION_TIMEOUT_MS)
            expand.click()
            # The click removes the "Развернуть" control itself (replaced
            # with "Свернуть") — waiting for it to disappear is the positive
            # signal that the fuller row set has rendered, not just guessed
            # at with a fixed sleep.
            expand.wait_for(state="hidden", timeout=ROW_HYDRATION_TIMEOUT_MS)
        except PlaywrightError:
            # #815 review: a stray failed click here (element temporarily
            # not interactable during a re-render, a slow network) must not
            # crash the caller — every caller already handles an
            # under-count as "read fewer rows than exist", the same
            # fail-safe behavior as any other unreadable row. Retrying is
            # deliberately NOT done here: the caller's own loop already
            # re-invokes this function on its next pass.
            pass


def _experience_row_indexes(page: Page) -> list[int]:
    """Return the indexes of currently rendered experience rows (#815/#833).

    EXPERIENCE_EDIT_BUTTON's ``{index}`` is an internal React counter shared
    across every editable block on the resume page, not the row's position:
    confirmed live indices were sparse and did not start at 0 (e.g. observed
    sets 2,3,4 and 1,6,7,8,12,17 on different resumes/reloads — no relation
    to the number of rows or their on-page order). Counting via
    ``range(0, N)`` and stopping at the first missing index (the pre-fix
    approach) silently undercounts — or returns 0 — whenever index 0 happens
    to be free, which is the common case once a resume has been edited a few
    times.

    Live testing also confirmed the set is STABLE across an open/cancel
    cycle on the same page (open one row's form, cancel it — the full index
    set returns unchanged): a snapshot taken once is safe to reuse for the
    rest of that read/edit pass, it does not need to be re-queried after
    every click. (An earlier draft of this fix assumed the set also shifts
    after an unrelated save elsewhere on the page — that was never actually
    observed and has been retracted; if a future reload/save DOES show the
    set changing, that would be a new, separate finding, not this one.)

    This enumerates the buttons that actually exist instead of guessing at
    their numbering, expanding a collapsed list first (#815) and waiting for
    the first row to hydrate after a fresh navigation/reload before reading
    (#833 — ``domcontentloaded``/``commit`` do not guarantee the React list
    has rendered yet, same "commit is not painted" pattern as elsewhere in
    this codebase).
    """
    buttons = page.locator(EXPERIENCE_EDIT_BUTTONS_ALL)
    if buttons.count() == 0:
        # #833: right after a reload/navigation, a resume that DOES have
        # experience rows may simply not have hydrated them into the DOM
        # yet ("commit"/"domcontentloaded" only confirm the URL/HTML
        # changed, not that the React list rendered — same pattern as
        # resume_position.py/skills.py/bump.py). count()==0 cannot tell
        # that apart from "this resume genuinely has zero experience rows",
        # so wait briefly for the first button to appear rather than
        # trusting an immediate read; a short, swallowed timeout here just
        # means the zero-rows case, which every caller already handles.
        try:
            buttons.first.wait_for(state="visible", timeout=ROW_HYDRATION_TIMEOUT_MS)
        except PlaywrightError:
            pass
    _expand_experience_list(page)
    buttons = page.locator(EXPERIENCE_EDIT_BUTTONS_ALL)
    indexes = []
    for i in range(buttons.count()):
        data_qa = buttons.nth(i).get_attribute("data-qa") or ""
        suffix = data_qa.rsplit("-", 1)[-1]
        if suffix.isdigit():
            indexes.append(int(suffix))
    return sorted(indexes)


def read_experience_on_hh(page: Page, resume_id: str) -> list[ExperienceEntry]:
    """Read existing rows through their confirmed editor fields, without save."""
    open_confirmed_resume(page, resume_id)
    # #815: row indexes are a non-contiguous internal React counter, not a
    # 0..N-1 range — iterate the actual indexes rather than range(count).
    # The company/position fields only exist in the DOM once that row's form
    # is open (count()==0 before the click), so a row's identity cannot be
    # read ahead of clicking it — the loop below reads it right after the
    # click instead. The index set itself was confirmed live to be STABLE
    # across an open/cancel cycle on the same page (open one row's form,
    # cancel it — the same full set comes back unchanged, see
    # `_experience_row_indexes()`), so one snapshot taken before the loop is
    # safe to iterate directly; it does not need to be re-queried per row.
    #
    # #844 live trace (2026-08-30): EXPERIENCE_EDIT_BUTTON's click is a full
    # SPA navigation to a separate page, /profile/edit/experience/{rowId}
    # ?resumeFrom=..., not an in-page modal on /resume/{resume_id} — the
    # third, previously unresearched experience-form DOM shape flagged as a
    # follow-up in PR #843. Two things follow from that:
    #   1. The company/position fields render only after that navigation
    #      completes, which is not instant — a bare _read() right after the
    #      click intermittently hit count()==0 (CLAUDE.md "commit is not
    #      painted" race). An explicit wait_for(visible) closes that race.
    #   2. On this page shape, clicking EXPERIENCE_CANCEL
    #      (data-qa='profile-layout-cancel-button', the same layout button
    #      reused by resume_education) was confirmed live to have NO effect
    #      at all: no frame navigation, no network request, no DOM change,
    #      tested via Playwright .click(), a native el.click() bypassing
    #      Playwright's actionability pipeline, and keyboard activation —
    #      the form and its unsaved company value stayed on screen for 30s+.
    #      Every row after the first therefore found its edit button gone
    #      (still parked on the unclosed form page) and read as empty. The
    #      only confirmed way back to the row list is the same navigation
    #      used to open it in the first place — open_confirmed_resume(),
    #      not the cancel button.
    result = []
    for index in _experience_row_indexes(page):
        try:
            page.locator(EXPERIENCE_EDIT_BUTTON.format(index=index)).click()
            company_locator = page.locator(EXPERIENCE_COMPANY.format(index=index))
            company_locator.wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            entry = ExperienceEntry(
                company=_read(company_locator),
                position=_read(page.locator(EXPERIENCE_POSITION.format(index=index))),
                start_year=_read(page.locator(EXPERIENCE_START_YEAR)),
                start_month=(
                    _read_month(page.locator(EXPERIENCE_START_MONTH))
                    if page.locator(EXPERIENCE_START_MONTH).count() == 1
                    else ""
                ),
                end_year=(
                    _read(page.locator(EXPERIENCE_END_YEAR))
                    if page.locator(EXPERIENCE_END_YEAR).count() == 1
                    else ""
                ),
                end_month=(
                    _read_month(page.locator(EXPERIENCE_END_MONTH))
                    if page.locator(EXPERIENCE_END_MONTH).count() == 1
                    else ""
                ),
                duties=_read(page.locator(EXPERIENCE_DESCRIPTION)),
                company_url=(
                    _read(page.locator(EXPERIENCE_COMPANY_URL))
                    if page.locator(EXPERIENCE_COMPANY_URL).count() == 1
                    else ""
                ),
            )
            result.append(entry)
        except (PlaywrightError, ValueError):
            # #796: a row can be unreadable in live DOM (drifted field, stray
            # non-experience card matching the indexed selector). Skip it
            # rather than failing the whole read — fill-mode stays usable
            # for the remaining rows instead of blocking on one bad row.
            pass
        # #844: EXPERIENCE_CANCEL does not work on this page shape (see
        # above) — navigate back to the resume page directly instead,
        # whether or not this row was read successfully, so the next
        # iteration's edit button is present in the DOM.
        try:
            open_confirmed_resume(page, resume_id)
        except (PlaywrightError, ValueError):
            break
    return result


def edit_experience_on_hh(
    page: Page, resume_id: str, plan: ExperiencePlan, *, dry_run: bool, indexes=None
):
    """Apply a plan to one or more rows; return structural row outcomes."""
    try:
        open_confirmed_resume(page, resume_id)
    except ValueError:
        return [ExperienceResult("identity резюме не подтверждён")]
    if plan.used_fallback and not plan.entries:
        return [ExperienceResult(plan.reason or "LLM не предложил безопасных изменений")]
    selected = list(indexes if indexes is not None else range(len(plan.entries)))
    results = []
    for entry, index in zip(plan.entries, selected, strict=False):
        trigger = page.locator(EXPERIENCE_EDIT_BUTTON.format(index=index))
        # #815: EXPERIENCE_EDIT_BUTTON's {index} is a non-contiguous internal
        # React counter, not a 0..N-1 row position — trigger.count()==0 no
        # longer means "this resume has no experience rows" the way it did
        # when indexes were assumed contiguous from 0. It now only means
        # "this particular index is not currently in use", which is true for
        # MOST indexes on any resume that has been edited before. Query the
        # actual set of existing rows instead: "first entry" (the only path
        # ever confirmed safe to CREATE a row, #786/#787) applies only when
        # the resume genuinely has zero rows.
        existing_indexes = _experience_row_indexes(page)
        first_entry = not existing_indexes
        if not first_entry and trigger.count() == 0:
            # A non-empty resume was asked to address an index that does not
            # exist among the current rows. #815 live testing found
            # /resume/edit/{resume_id}/experience — the only route this
            # module ever used for "no trigger found" — does NOT reliably
            # create a new row on a resume that already has entries: it can
            # open blank, or it can silently rebind to and overwrite an
            # unrelated existing row matched by some other identity (start
            # date, in the observed case). Neither outcome is safe to walk
            # into automatically, so this fails closed rather than reusing
            # the first-entry route on a resume where it was never confirmed
            # safe.
            return results + [
                ExperienceResult(
                    f"строка опыта {index}: индекс не найден среди существующих строк "
                    f"({existing_indexes}) — добавление новой записи к резюме, где опыт "
                    "уже есть, не подтверждено безопасным (#815)"
                )
            ]
        # #796: snapshot the row count BEFORE opening the form for this entry
        # — taking it after save cannot distinguish a bound save from a
        # silent no-op, since both leave the count read at the same time.
        before_indexes = set(existing_indexes)
        if first_entry:
            # #786/#787: no in-page "add" trigger for the first experience row
            # was ever confirmed by a research dump — the visible suggestion
            # chip (`suitable-vacancies-suggest-item-experience`) navigates to
            # the *shared* profile editor (`/profile/edit/experience`) without
            # a reliable `resumeFrom` binding on an incomplete resume (#787
            # live confirmation: the query param was dropped entirely). The
            # resume-scoped route below was confirmed live (#787 write test)
            # to open the form directly, pre-bound to this resume_id, with no
            # click or checkbox panel involved.
            edit_path = f"/resume/edit/{resume_id}/experience"
            try:
                goto_hh(page, f"{HH_BASE_URL}{edit_path}")
            except PlaywrightError as exc:
                return results + [
                    ExperienceResult(
                        f"строка опыта {index}: не удалось открыть новую запись: {exc}"
                    )
                ]
            if urlsplit(page.url).path.rstrip("/") != edit_path:
                return results + [
                    ExperienceResult(
                        f"строка опыта {index}: форма открыта не для того резюме ({page.url})"
                    )
                ]
        elif trigger.count() != 1:
            return results + [
                ExperienceResult(f"строка опыта {index}: триггер не найден однозначно")
            ]
        # #786/#787: the first-row editor at /resume/edit/{id}/experience is a
        # distinct DOM shape from the indexed row editor — separate company/
        # position/save/cancel data-qa values (start/end year and description
        # are the same non-indexed selectors on both shapes, confirmed live).
        company_selector = (
            FIRST_EXPERIENCE_COMPANY if first_entry else EXPERIENCE_COMPANY.format(index=index)
        )
        position_selector = (
            FIRST_EXPERIENCE_POSITION if first_entry else EXPERIENCE_POSITION.format(index=index)
        )
        save_selector = FIRST_EXPERIENCE_SAVE if first_entry else EXPERIENCE_SAVE
        cancel_selector = FIRST_EXPERIENCE_CANCEL if first_entry else EXPERIENCE_CANCEL
        save_attempted = False
        try:
            if not first_entry:
                trigger.click()
            page.locator(company_selector).wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            _fill(page.locator(company_selector), entry.company)
            _fill(page.locator(position_selector), entry.position)
            _fill(page.locator(EXPERIENCE_START_YEAR), entry.start_year)
            start_month_locator = page.locator(EXPERIENCE_START_MONTH)
            if start_month_locator.count() == 1 and entry.start_month:
                _select_month(page, start_month_locator, entry.start_month)
            end_year_locator = page.locator(EXPERIENCE_END_YEAR)
            end_month_locator = page.locator(EXPERIENCE_END_MONTH)
            if end_year_locator.count() == 1:
                # #800: the end-year field is disabled while the "Работаю
                # сейчас" checkbox is checked (checked by default on a new
                # entry). Filling a disabled field just retries fill() until
                # Playwright's timeout — check is_enabled() first rather than
                # attempting the fill unconditionally. #811: end-month shares
                # the exact same disabled/enabled gating (confirmed live), so
                # it is filled right alongside end-year in each branch below.
                end_year_enabled = end_year_locator.is_enabled()
                if entry.current:
                    if end_year_enabled:
                        _fill(end_year_locator, "")
                    # else: already disabled/blank — nothing to do.
                elif end_year_enabled:
                    _fill(end_year_locator, entry.end_year)
                    if end_month_locator.count() == 1 and entry.end_month:
                        _select_month(page, end_month_locator, entry.end_month)
                elif first_entry:
                    # Checkbox selector is confirmed only on the first-row
                    # editor's distinct DOM shape (#800) — uncheck it to
                    # unlock the end-date fields before filling.
                    checkbox = page.locator(FIRST_EXPERIENCE_CURRENT_CHECKBOX)
                    if checkbox.count() != 1:
                        return results + [
                            ExperienceResult(
                                f"строка {index}: чекбокс 'Работаю сейчас' "
                                "не подтверждён однозначно"
                            )
                        ]
                    checkbox.click()
                    # No wait_for(state=...) covers "enabled" specifically —
                    # the field is already visible while disabled, so that
                    # would be a no-op. fill()'s own actionability check
                    # already waits for enabled (with its own timeout), so
                    # nothing further is needed here.
                    _fill(end_year_locator, entry.end_year)
                    if end_month_locator.count() == 1 and entry.end_month:
                        _select_month(page, end_month_locator, entry.end_month)
                else:
                    # Indexed row editor: no confirmed checkbox selector for
                    # this DOM shape — fail closed rather than guess.
                    return results + [
                        ExperienceResult(
                            f"строка {index}: поле окончания заблокировано, чекбокс "
                            "'Работаю сейчас' для этой формы не подтверждён"
                        )
                    ]
            _fill(page.locator(EXPERIENCE_DESCRIPTION), entry.description())
            if entry.company_url and page.locator(EXPERIENCE_COMPANY_URL).count() == 1:
                _fill(page.locator(EXPERIENCE_COMPANY_URL), entry.company_url)
            if dry_run:
                results.append(ExperienceResult(f"строка {index}: предложено, save не нажат", True))
                page.locator(cancel_selector).click()
            else:
                save = page.locator(save_selector)
                if save.count() != 1:
                    return results + [
                        ExperienceResult(f"строка {index}: save-кнопка не подтверждена")
                    ]
                save_attempted = True
                save.click()
                try:
                    page.wait_for_url(
                        f"**/resume/{resume_id}",
                        wait_until="commit",
                        timeout=SAVE_TIMEOUT_MS,
                    )
                except PlaywrightError as exc:
                    return results + [
                        ExperienceResult(
                            f"строка {index}: сохранение не подтверждено после клика: {exc}",
                            uncertain=True,
                        )
                    ]
                if not resume_identity_matches(page, resume_id):
                    return results + [
                        ExperienceResult(
                            f"строка {index}: после save identity резюме не подтверждён",
                            uncertain=True,
                        )
                    ]
                # #796/#787: a click succeeding and landing back on the resume
                # page is not proof the row is bound to THIS resume — #787
                # found saves that silently went to the shared profile
                # instead. Reload and re-read the actual row set rather than
                # trusting the in-memory DOM state right after save. This
                # only applies to a genuinely new row (first_entry): editing
                # an EXISTING row in place (fill mode re-saves the same
                # index) must not be flagged just because the row set didn't
                # change.
                #
                # #815 review: a bare count() comparison (before vs. after)
                # is a weak positive signal — a resume that lost one row and
                # gained a different one elsewhere on the same page (e.g. an
                # unrelated concurrent edit) would show the same count and
                # false-pass. The row set's *contents* is not directly
                # comparable either — EXPERIENCE_EDIT_BUTTON's {index} is an
                # internal React counter, so "a new index exists" is not the
                # same claim as "our indexed one exists" if hh.ru is mid
                # re-render across the reload. What IS decisive: at least one
                # index present now that was not present before the save —
                # that is only possible if hh.ru actually created a new row.
                try:
                    page.reload(wait_until="domcontentloaded")
                    require_authenticated_page(page)
                    if not resume_identity_matches(page, resume_id):
                        return results + [
                            ExperienceResult(
                                f"строка {index}: после reload identity резюме не подтверждён",
                                uncertain=True,
                            )
                        ]
                    after_indexes = set(_experience_row_indexes(page))
                except (PlaywrightError, NotAuthenticated) as exc:
                    return results + [
                        ExperienceResult(
                            f"строка {index}: post-save проверка не подтверждена: {exc}",
                            uncertain=True,
                        )
                    ]
                if first_entry and not (after_indexes - before_indexes):
                    return results + [
                        ExperienceResult(
                            f"строка {index}: запись не привязалась к резюме "
                            f"(строк до={sorted(before_indexes)}, "
                            f"после={sorted(after_indexes)})"
                        )
                    ]
                results.append(
                    ExperienceResult(f"строка {index}: сохранено и привязано к резюме", True)
                )
        except (PlaywrightError, ValueError) as exc:
            return results + [
                ExperienceResult(
                    f"строка {index}: {exc}",
                    uncertain=save_attempted,
                )
            ]
    return results

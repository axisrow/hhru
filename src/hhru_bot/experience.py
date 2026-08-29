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
    EXPERIENCE_END_YEAR,
    EXPERIENCE_POSITION,
    EXPERIENCE_SAVE,
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


@dataclass
class ExperienceEntry:
    company: str = ""
    position: str = ""
    start_year: str = ""
    end_year: str = ""
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
        for key in ("company", "position", "start_year", "end_year", "duties", "company_url")
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
        "объектов с полями company, position, start_year, end_year, current, duties, "
        "achievements, company_url. Не выдумывай факты, даты, метрики или URL: "
        "используй только сведения пользователя. achievements — список строк. "
        "current=true означает работу по настоящее время."
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
    return ExperiencePlan(entries)


def _merge_fill_plan(
    existing: list[ExperienceEntry], proposed: list[ExperienceEntry]
) -> list[ExperienceEntry] | None:
    """Keep identity fields and existing text authoritative in ``fill`` mode."""
    if len(existing) != len(proposed):
        return None
    merged = []
    for old, new in zip(existing, proposed, strict=True):
        protected = ("company", "position", "start_year", "end_year", "current", "company_url")
        if any(getattr(old, key) != getattr(new, key) for key in protected):
            return None
        merged.append(
            ExperienceEntry(
                company=old.company,
                position=old.position,
                start_year=old.start_year,
                end_year=old.end_year,
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


def _count_experience_rows(page: Page) -> int:
    """Count existing rows via the confirmed indexed edit-trigger selector."""
    count = 0
    while page.locator(EXPERIENCE_EDIT_BUTTON.format(index=count)).count() == 1:
        count += 1
    return count


def read_experience_on_hh(page: Page, resume_id: str) -> list[ExperienceEntry]:
    """Read existing rows through their confirmed editor fields, without save."""
    open_confirmed_resume(page, resume_id)
    count = _count_experience_rows(page)
    result = []
    for index in range(count):
        try:
            page.locator(EXPERIENCE_EDIT_BUTTON.format(index=index)).click()
            entry = ExperienceEntry(
                company=_read(page.locator(EXPERIENCE_COMPANY.format(index=index))),
                position=_read(page.locator(EXPERIENCE_POSITION.format(index=index))),
                start_year=_read(page.locator(EXPERIENCE_START_YEAR)),
                end_year=(
                    _read(page.locator(EXPERIENCE_END_YEAR))
                    if page.locator(EXPERIENCE_END_YEAR).count() == 1
                    else ""
                ),
                duties=_read(page.locator(EXPERIENCE_DESCRIPTION)),
                company_url=(
                    _read(page.locator(EXPERIENCE_COMPANY_URL))
                    if page.locator(EXPERIENCE_COMPANY_URL).count() == 1
                    else ""
                ),
            )
            page.locator(EXPERIENCE_CANCEL).click()
            result.append(entry)
        except (PlaywrightError, ValueError):
            # #796: a row can be unreadable in live DOM (drifted field, stray
            # non-experience card matching the indexed selector). Skip it
            # rather than failing the whole read — fill-mode stays usable
            # for the remaining rows instead of blocking on one bad row.
            try:
                page.locator(EXPERIENCE_CANCEL).click()
            except PlaywrightError:
                pass
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
        first_entry = trigger.count() == 0
        # #796: snapshot the row count BEFORE opening the form for this entry
        # — taking it after save cannot distinguish a bound save from a
        # silent no-op, since both leave the count read at the same time.
        before_count = _count_experience_rows(page)
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
            end_year_locator = page.locator(EXPERIENCE_END_YEAR)
            if end_year_locator.count() == 1:
                # #800: the end-year field is disabled while the "Работаю
                # сейчас" checkbox is checked (checked by default on a new
                # entry). Filling a disabled field just retries fill() until
                # Playwright's timeout — check is_enabled() first rather than
                # attempting the fill unconditionally.
                end_year_enabled = end_year_locator.is_enabled()
                if entry.current:
                    if end_year_enabled:
                        _fill(end_year_locator, "")
                    # else: already disabled/blank — nothing to do.
                elif end_year_enabled:
                    _fill(end_year_locator, entry.end_year)
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
                    end_year_locator.wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
                    _fill(end_year_locator, entry.end_year)
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
                # instead. Reload and recount rather than trusting the
                # in-memory DOM state right after save. The count-growth
                # check only applies to a genuinely new row (first_entry):
                # editing an EXISTING row in place (fill mode re-saves the
                # same index) never grows the count and must not be flagged.
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
                    after_count = _count_experience_rows(page)
                except (PlaywrightError, NotAuthenticated) as exc:
                    return results + [
                        ExperienceResult(
                            f"строка {index}: post-save проверка не подтверждена: {exc}",
                            uncertain=True,
                        )
                    ]
                if first_entry and after_count <= before_count:
                    return results + [
                        ExperienceResult(
                            f"строка {index}: запись не привязалась к резюме "
                            f"(строк до={before_count}, после={after_count})"
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

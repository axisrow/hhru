"""Typed planning and UI editing for supported additional resume sections."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import (
    HH_BASE_URL,
    RESUME_UNAVAILABLE_REASON,
    PageStateIndeterminate,
    goto_hh,
    has_auth_cookie,
    has_login_form,
    has_resume_error_banner,
    labelled_field,
    open_hydrated_resume_editor,
)

if TYPE_CHECKING:
    from .config_sections.ai_profile import AIProfile
    from .config_sections.resume_sections import ResumeSectionsConfig

logger = logging.getLogger("hhru_bot.resume_sections")
FORM_TIMEOUT_MS = 10_000

# Потолок ожидания закрытия inline-редактора после save (#331).
SAVE_TIMEOUT_MS = 30_000

RESUME_EDIT_BUTTON = {
    "attestations": "[data-qa^='resume-edit-button-attestationEducation-']",
    "recommendations": "[data-qa^='resume-edit-button-recommendation-']",
}
# Both section editors embed the resume id in their route, so both patterns are
# bound per-call rather than kept static: a stale or misdirected edit link for a
# DIFFERENT resume must not pass the route guard (#368 cycle-review round 1,
# codex finding).
#
# The attestation entry used to be static and pointed at /profile/edit/... — the
# same live read-only probe that renamed ATTESTATION_FIELDS below (#703,
# 2026-08-30) showed the real route is /resume/edit/<resume_id>/
# attestationEducation/<n>. The guard therefore never matched, and opening an
# attestation editor always failed with wrong_route_error; fixing the field
# names alone would not have made this path work.


def _attestation_route(resume_id: str) -> re.Pattern[str]:
    return re.compile(rf"/resume/edit/{re.escape(resume_id)}/attestationEducation(?:/[^/?#]+)?")


def _recommendation_route(resume_id: str) -> re.Pattern[str]:
    return re.compile(rf"/resume/edit/{re.escape(resume_id)}/recommendation(?:/[^/?#]+)?")


SECTION_ROUTES = {
    "attestations": _attestation_route,
    "recommendations": _recommendation_route,
}

FIRST_SECTION_EDIT_PATHS = {
    "attestations": "attestationEducation",
    "recommendations": "recommendation",
}
# Live-confirmed on the empty draft probe (2026-09-02): these suggestion
# buttons are rendered for an actually empty block.  Require the corresponding
# marker before treating zero row triggers as an empty section; otherwise a
# hydration/anti-bot failure could be mistaken for permission to create a row.
EMPTY_SECTION_MARKERS = {
    "attestations": "[data-qa='suitable-vacancies-suggest-item-attestationEducation']",
    "recommendations": "[data-qa='suitable-vacancies-suggest-item-recommendation']",
}


# Live-confirmed 2026-08-30 on draft resume a1d75539… at
# /resume/edit/<id>/attestationEducation: the form DOES expose data-qa on every
# input, but under a different family than the historical candidates
# (``profile-education-attestation-*`` / ``profile-education-year-input``, all
# count=0 there).  Note the third field: hh.ru labels it "Специализация" but
# names the attribute ``-result``; keep the code's field order aligned with
# Attestation, not with the attribute wording.
ATTESTATION_FIELDS = (
    "resume-attestation-education-input-name",
    "resume-attestation-education-input-organization",
    "resume-attestation-education-input-result",
    "resume-attestation-education-input-year",
)


@dataclass(frozen=True)
class Attestation:
    name: str
    organization: str
    specialty: str
    year: str


@dataclass(frozen=True)
class Recommendation:
    text: str
    company: str
    name: str = ""
    position: str = ""


@dataclass
class ResumeSectionsPlan:
    attestations: list[Attestation] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Ручные строки блоков #1118 (--contact/--certificate/--portfolio/--link).
    # Блоковые ишью (#1119-1122) добавляют типизированные dataclass + fill-row
    # и переносят свои строки из manual в типизированные поля выше.
    manual: list[ManualRow] = field(default_factory=list)


# --- ручной per-row контракт (#1118) ----------------------------------------

# Статусы исхода строки. `updated` зарезервирован: обновление требует
# readback существующих строк на странице резюме (доменная разведка — задача
# блоковых ишью #1119-1122); этот фундамент update не выполняет никогда.
OUTCOME_PLANNED = "planned"
OUTCOME_APPENDED = "appended"
OUTCOME_UPDATED = "updated"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class ManualRow:
    """Одна ручная строка произвольного поддержанного блока.

    Поля уже строго провалидированы парсером команды: ключи совпадают со
    схемой блока, значения — непустые либо отсутствуют.
    """

    block: str
    fields: dict[str, str]


# Предварительные схемы ручных блоков. Минимальные наборы из эпика #1117;
# ТОЧНЫЕ поля фиксирует read-only census живой формы (#1119-1122) — блоковое
# ишью сужает/расширяет кортеж по факту, парсер строг к лишним ключам уже
# здесь, так что смена схемы не ослабляет валидацию.
MANUAL_BLOCK_SCHEMAS: dict[str, tuple[str, ...]] = {
    "contact": ("name", "value"),
    "certificate": ("name", "organization", "year"),
    "portfolio": ("name", "url"),
    "link": ("name", "url"),
}


@dataclass(frozen=True)
class RowOutcome:
    """Исход одной строки плана (#1118, per-row контракт).

    Честный итог команды: частичный успех одной строки не выдаётся за успех
    всего блока — неудачная строка несёт status=failed с причиной, а команды
    завершаются ненулевым кодом при любом failed.
    """

    block: str
    index: int
    status: str
    reason: str = ""


def _dedupe(block: str, items: list, key_of) -> tuple[list, list[RowOutcome]]:
    """Append/dedup семантика #1118: полное совпадение ВСЕХ полей строки внутри
    одного блока — это duplicate, вторая запись не планируется. Молчаливой
    перезаписи/удаления существующих строк нет: обновление существующей строки
    — только по явному ключу совпадения и только там, где блоковое ишью
    реализует readback существующих строк; этот фундамент новые строки только
    добавляет (appended/planned), никогда не переписывает."""
    kept: list = []
    outcomes: list[RowOutcome] = []
    seen: dict = {}
    for index, item in enumerate(items):
        key = key_of(item)
        if key in seen:
            outcomes.append(
                RowOutcome(block, index, OUTCOME_DUPLICATE, f"повтор строки {seen[key]}")
            )
        else:
            seen[key] = index
            kept.append(item)
            outcomes.append(RowOutcome(block, index, OUTCOME_PLANNED))
    return kept, outcomes


def plan_from_rows(rows: list[ManualRow]) -> tuple[ResumeSectionsPlan, list[RowOutcome]]:
    """Build a manual plan without LLM (#1118). Тот же ResumeSectionsPlan, что
    и у LLM-пути; LLM-путь (build_messages/generate_plan) не затрагивается.
    Дедуп — один канон `_dedupe`, прогнанный per-block (cycle-review PR #1125);
    исходы сохраняют исходный порядок строк."""
    plan = ResumeSectionsPlan()
    outcomes: list[RowOutcome | None] = [None] * len(rows)
    by_block: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row.block not in MANUAL_BLOCK_SCHEMAS:
            outcomes[index] = RowOutcome(
                row.block, index, OUTCOME_FAILED, f"неизвестный блок {row.block!r}"
            )
        else:
            by_block.setdefault(row.block, []).append(index)
    for block, indices in by_block.items():
        kept, block_outcomes = _dedupe(
            block,
            [rows[index] for index in indices],
            lambda row: tuple(sorted(row.fields.items())),
        )
        plan.manual.extend(kept)
        for local_index, outcome in zip(indices, block_outcomes, strict=True):
            outcomes[local_index] = RowOutcome(block, local_index, outcome.status, outcome.reason)
    return plan, [outcome for outcome in outcomes if outcome is not None]


def fail_tail(outcomes: list[RowOutcome] | None, block: str, start: int, reason: str) -> None:
    """Пометить строки [start; конец] блока failed — запись блока остановлена."""
    if outcomes is None:
        return
    for index in range(start, len(outcomes)):
        outcomes[index] = RowOutcome(block, index, OUTCOME_FAILED, reason)


def _profile_text(profile: AIProfile | None) -> str:
    if profile is None:
        return ""
    return "\n".join(
        part
        for part in (
            getattr(profile, "summary", ""),
            getattr(profile, "desired_role", ""),
            "Навыки: " + ", ".join(getattr(profile, "skills", [])),
            "Достижения: " + "; ".join(getattr(profile, "highlights", [])),
        )
        if part
    )


def build_messages(config: ResumeSectionsConfig, profile: AIProfile | None) -> list[dict[str, str]]:
    """Build a strict JSON-only prompt; user text is context, not instructions."""
    system = (
        "Сформируй дополнительные разделы резюме. Ответь только JSON-объектом с "
        "массивами attestations и recommendations. Не выдумывай факты: неизвестное "
        "оставляй пустым. Каждая аттестация: name, organization, specialty, year. "
        "Каждая рекомендация: company, name, position. Поле text не поддерживается "
        "текущей формой HH.ru и не будет сохранено (#367) — не заполняй его."
    )
    user = (
        f"Режим: {config.mode}. Нужные блоки: {', '.join(config.blocks)}.\n"
        f"Данные пользователя:\n{_profile_text(profile)}\n{config.context}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_plan(content: str | None, blocks: list[str]) -> ResumeSectionsPlan:
    """Parse LLM output fail-closed; malformed output produces no writes."""
    plan = ResumeSectionsPlan()
    try:
        raw = json.loads(content or "")
        if not isinstance(raw, dict):
            raise ValueError("JSON должен быть объектом")
        if "attestations" in blocks:
            for item in raw.get("attestations", []):
                if isinstance(item, dict):
                    plan.attestations.append(
                        Attestation(
                            *(
                                _text(item.get(k))
                                for k in ("name", "organization", "specialty", "year")
                            )
                        )
                    )
        if "recommendations" in blocks:
            for item in raw.get("recommendations", []):
                if isinstance(item, dict):
                    plan.recommendations.append(
                        Recommendation(
                            *(_text(item.get(k)) for k in ("text", "company", "name", "position"))
                        )
                    )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        plan.skipped.append(f"ответ LLM не разобран: {exc}")
    return plan


def generate_plan(
    llm_client, config: ResumeSectionsConfig, profile: AIProfile | None
) -> ResumeSectionsPlan:
    try:
        response = llm_client.chat(
            build_messages(config, profile), temperature=0.2, max_tokens=1200
        )
    except Exception as exc:  # noqa: BLE001 - LLM failure must not become a write
        logger.warning("LLM additional-sections failed: %s", exc)
        return ResumeSectionsPlan(skipped=[f"LLM недоступен: {exc}"])
    return parse_plan(getattr(response, "content", None), config.blocks)


def _fill(locator, value: str) -> None:
    if not value:
        return
    locator.fill(value)


def _fill_attestation_row(page: Page, item: Attestation) -> Locator:
    for qa_field, value in zip(ATTESTATION_FIELDS, item.__dict__.values(), strict=True):
        _fill(page.locator(f"[data-qa='{qa_field}']"), value)
    # The attestation editor is a resume-scoped partial edit, not the profile
    # layout: live probe 2026-08-30 found resume-partial-edit-save (count=1)
    # and no profile-layout-save-button on this screen.
    return page.locator("[data-qa='resume-partial-edit-save']")


def _fill_recommendation_row(page: Page, item: Recommendation) -> Locator:
    if item.text:
        raise PlaywrightError(
            "текущая форма рекомендации не содержит поля текста; запись остановлена"
        )

    def labelled(label: str):
        try:
            return labelled_field(page, label)
        except PageStateIndeterminate as exc:
            raise PlaywrightError(f"поле рекомендации {label!r} не найдено однозначно") from exc

    _fill(labelled("Имя человека"), item.name)
    _fill(labelled("Должность"), item.position)
    company = page.locator("input[name='company']")
    if company.count() != 1:
        raise PlaywrightError("поле рекомендации 'Организация' не найдено однозначно")
    _fill(company, item.company)
    return page.locator("[data-qa='resume-partial-edit-save']")


def _apply_rows(
    page: Page,
    block: str,
    items: list[Attestation] | list[Recommendation],
    fill_row,
    *,
    resume_id: str = "",
    dry_run: bool,
    outcomes: list[RowOutcome] | None = None,
) -> list[str]:
    errors: list[str] = []
    if outcomes is not None and len(outcomes) != len(items):
        raise ValueError("outcomes должен быть выровнен по строкам блока")
    if resume_id and items:
        # A previous empty-section editor may leave the page on its own route
        # after cancel/save.  Re-open the resume before inspecting this block
        # so its row trigger and empty marker are always read from the same
        # deterministic page (#922).
        goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
        if has_login_form(page):
            return ["hh.ru показал форму входа"]
    trigger = page.locator(RESUME_EDIT_BUTTON[block])
    for index, item in enumerate(items):
        # The current HH.ru recommendation editor has no text control. Reject
        # such rows before opening the editor so fail-closed handling cannot
        # leave a partially opened form behind (#367).
        if block == "recommendations" and getattr(item, "text", ""):
            reason = "текущая форма рекомендации не содержит поля текста; запись остановлена"
            errors.append(f"{block}: строка {index} не подтверждена: {reason}")
            if outcomes is not None:
                outcomes[index] = RowOutcome(block, index, OUTCOME_FAILED, reason)
            fail_tail(outcomes, block, index + 1, "запись блока остановлена")
            break
        ready_selector = (
            f"[data-qa='{ATTESTATION_FIELDS[0]}']"
            if block == "attestations"
            else "input[name='company']"
        )
        try:
            # trigger.count() itself can raise on iterations after a previous
            # row's save.click() already succeeded (#352/codex round 3) — the
            # whole per-row body must stay inside this guard, not just the
            # click/wait_for, so no browser call here can escape apply_plan
            # uncaught and hide which earlier rows already saved. This also
            # covers save.click()/cancel.click() themselves (#331 cycle-review
            # round 3): an element-detached or navigation error from either
            # must not propagate and crash apply_plan.
            trigger_count = trigger.count()
            if index == 0 and trigger_count == 0 and resume_id:
                # An empty section has no row trigger.  HH.ru's confirmed
                # resume-scoped editor route opens the first row directly;
                # unlike a suggestion chip, it cannot silently bind the row
                # to another resume.  This is the only creation path here:
                # later missing rows remain rejected below.
                empty_marker = page.locator(EMPTY_SECTION_MARKERS[block])
                if empty_marker.count() != 1:
                    raise RuntimeError(f"{block}: пустой блок не подтверждён однозначно")
                edit_path = f"/resume/edit/{resume_id}/{FIRST_SECTION_EDIT_PATHS[block]}"
                goto_hh(page, f"{HH_BASE_URL}{edit_path}")
                current_path = urlsplit(page.url).path.rstrip("/")
                if not SECTION_ROUTES[block](resume_id).fullmatch(current_path):
                    raise RuntimeError(f"{block}: первая строка открыта не для того резюме")
                page.locator(ready_selector).wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            elif index >= trigger_count:
                errors.append(f"{block}: строка {index} отсутствует; добавление не подтверждено")
                if outcomes is not None:
                    outcomes[index] = RowOutcome(
                        block, index, OUTCOME_FAILED, "строка отсутствует на странице"
                    )
                continue
            elif resume_id:
                edit_path = SECTION_ROUTES[block](resume_id)
                open_hydrated_resume_editor(
                    page,
                    trigger_selector=trigger.nth(index),
                    editor_selector=ready_selector,
                    profile_path=f"/resume/{resume_id}",
                    edit_path=edit_path,
                    click_trigger=True,
                    timeout=FORM_TIMEOUT_MS,
                    trigger_error=f"{block}: строка {index} не найдена однозначно",
                    open_error=f"{block}: строка {index} не открылась",
                    wrong_route_error=f"{block}: строка {index} открыта не для того резюме",
                )
            else:
                # Keep the pure unit fake focused on row-level error handling;
                # live callers always provide resume_id and use the hydrated
                # editor helper above.
                trigger.nth(index).click()
                page.locator(ready_selector).wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            save = fill_row(page, item)
            if not dry_run:
                if save.count() != 1:
                    errors.append(f"{block}: неоднозначная кнопка сохранения")
                    if outcomes is not None:
                        outcomes[index] = RowOutcome(
                            block, index, OUTCOME_FAILED, "неоднозначная кнопка сохранения"
                        )
                    fail_tail(outcomes, block, index + 1, "запись блока остановлена")
                    # The row editor is left open in this state; querying the
                    # next trigger against it would be unreliable (#331).
                    break
                # The page is already on /resume/{resume_id} before this click
                # (see apply_plan below), and a successful save closes the
                # inline editor in place without changing the URL — so
                # page.wait_for_url() against that same URL would resolve
                # immediately regardless of whether the save actually
                # succeeded (#331: false-positive success). The editor
                # closing (the save button disappearing) is the positive,
                # save-specific signal instead. A timeout here means the
                # editor is likely still open (same rationale as the ambiguous
                # save/cancel branches below), so it falls through to the
                # shared except below and stops the block, rather than
                # clicking the next row's trigger against an unresolved
                # editor state (#331, codex+claude cycle-review round 2).
                try:
                    save.click()
                    save.wait_for(state="hidden", timeout=SAVE_TIMEOUT_MS)
                    if outcomes is not None:
                        outcomes[index] = RowOutcome(
                            block, index, OUTCOME_APPENDED, "сохранение подтверждено"
                        )
                except (PlaywrightError, RuntimeError) as exc:
                    raise PlaywrightError(
                        f"сохранение не подтверждено (uncertain) после клика: {exc}"
                    ) from exc
            else:
                # Leave the row editor before moving to the next row.  Otherwise
                # the next trigger is queried while the previous form is still open.
                # Both supported blocks render the resume-scoped partial editor,
                # so the cancel control is the same for each (live probe
                # 2026-08-30: resume-partial-edit-cancel count=1 on the
                # attestation form, profile-layout-cancel-button count=0).
                cancel = page.locator("[data-qa='resume-partial-edit-cancel']")
                if cancel.count() != 1:
                    errors.append(f"{block}: неоднозначная кнопка отмены")
                    if outcomes is not None:
                        outcomes[index] = RowOutcome(
                            block, index, OUTCOME_FAILED, "неоднозначная кнопка отмены"
                        )
                    fail_tail(outcomes, block, index + 1, "запись блока остановлена")
                    # Same reasoning as the save branch: the editor stays open,
                    # so stop this block instead of leaving it open (#331).
                    break
                cancel.click()
        except (PlaywrightError, RuntimeError) as exc:
            # A hydration timeout here may follow an already-successful save.click()
            # on a previous row (#352/codex round 3), including a save.wait_for
            # timeout right after save.click() (#331/codex+claude): fail closed
            # with an explicit error for this row and stop the block instead of
            # letting the exception escape apply_plan and hide which earlier
            # rows already saved.
            errors.append(f"{block}: строка {index} не подтверждена: {exc}")
            if outcomes is not None:
                outcomes[index] = RowOutcome(block, index, OUTCOME_FAILED, str(exc))
            fail_tail(outcomes, block, index + 1, "запись блока остановлена")
            break
    return errors


def apply_plan(
    page: Page,
    resume_id: str,
    plan: ResumeSectionsPlan,
    *,
    dry_run: bool,
    outcomes: dict[str, list[RowOutcome]] | None = None,
) -> list[str]:
    """Apply rows, creating the first row through the confirmed empty-section route.

    outcomes (#1118) — необязательный per-row контракт: словарь «блок → список
    RowOutcome, выровненный по строкам плана». Ранний выход (нет auth, форма
    входа, сбойный экран) честно помечает ВСЕ строки failed — ни одна строка
    не остаётся «planned» при ненулевом итоге команды.
    """

    def _fail_all(reason: str) -> None:
        if outcomes is None:
            return
        for block in ("attestations", "recommendations"):
            fail_tail(outcomes.get(block), block, 0, reason)

    if not has_auth_cookie(page):
        _fail_all("отсутствует auth cookie")
        return ["отсутствует auth cookie"]
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
    if has_login_form(page):
        _fail_all("hh.ru показал форму входа")
        return ["hh.ru показал форму входа"]
    # #972: сбойный экран /resume/{id} — внятный отказ вместо таймаута на
    # поиске триггеров секций. Pre-mutation, обычный failed/retry.
    if has_resume_error_banner(page):
        _fail_all(RESUME_UNAVAILABLE_REASON)
        return [RESUME_UNAVAILABLE_REASON]
    errors = list(plan.skipped)
    errors += _apply_rows(
        page,
        "attestations",
        plan.attestations,
        _fill_attestation_row,
        resume_id=resume_id,
        dry_run=dry_run,
        outcomes=None if outcomes is None else outcomes.get("attestations"),
    )
    errors += _apply_rows(
        page,
        "recommendations",
        plan.recommendations,
        _fill_recommendation_row,
        resume_id=resume_id,
        dry_run=dry_run,
        outcomes=None if outcomes is None else outcomes.get("recommendations"),
    )
    return errors

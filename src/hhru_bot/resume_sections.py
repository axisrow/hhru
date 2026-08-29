"""Typed planning and UI editing for supported additional resume sections."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import (
    HH_BASE_URL,
    PageStateIndeterminate,
    goto_hh,
    has_auth_cookie,
    has_login_form,
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
SECTION_ROUTES = {
    "attestations": re.compile(r"/profile/edit/attestationEducation/[^/?#]+"),
    # The attestation route has no resume id in it (only an attestation id),
    # so its pattern is static. The recommendations route embeds the resume id
    # itself — bind it per-call via _recommendation_route() so a stale or
    # misdirected edit link for a DIFFERENT resume cannot pass the route guard
    # (#368 cycle-review round 1, codex finding: the previous static pattern
    # matched any resume id here despite wrong_route_error's identity claim).
}


def _recommendation_route(resume_id: str) -> re.Pattern[str]:
    return re.compile(rf"/resume/edit/{re.escape(resume_id)}/recommendation/[^/?#]+")


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
) -> list[str]:
    errors: list[str] = []
    trigger = page.locator(RESUME_EDIT_BUTTON[block])
    for index, item in enumerate(items):
        # The current HH.ru recommendation editor has no text control. Reject
        # such rows before opening the editor so fail-closed handling cannot
        # leave a partially opened form behind (#367).
        if block == "recommendations" and getattr(item, "text", ""):
            errors.append(
                f"{block}: строка {index} не подтверждена: "
                "текущая форма рекомендации не содержит поля текста; запись остановлена"
            )
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
            if index >= trigger.count():
                errors.append(f"{block}: строка {index} отсутствует; добавление не подтверждено")
                continue
            if resume_id:
                edit_path = (
                    _recommendation_route(resume_id)
                    if block == "recommendations"
                    else SECTION_ROUTES[block]
                )
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
            break
    return errors


def apply_plan(page: Page, resume_id: str, plan: ResumeSectionsPlan, *, dry_run: bool) -> list[str]:
    """Apply only existing, live-confirmed rows. Never invents add controls."""
    if not has_auth_cookie(page):
        return ["отсутствует auth cookie"]
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume_id}")
    if has_login_form(page):
        return ["hh.ru показал форму входа"]
    errors = list(plan.skipped)
    errors += _apply_rows(
        page,
        "attestations",
        plan.attestations,
        _fill_attestation_row,
        resume_id=resume_id,
        dry_run=dry_run,
    )
    errors += _apply_rows(
        page,
        "recommendations",
        plan.recommendations,
        _fill_recommendation_row,
        resume_id=resume_id,
        dry_run=dry_run,
    )
    return errors

"""Typed planning and UI editing for supported additional resume sections."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import HH_BASE_URL, goto_hh, has_auth_cookie, has_login_form

if TYPE_CHECKING:
    from .config_sections.ai_profile import AIProfile
    from .config_sections.resume_sections import ResumeSectionsConfig

logger = logging.getLogger("hhru_bot.resume_sections")
FORM_TIMEOUT_MS = 10_000

RESUME_EDIT_BUTTON = {
    "attestations": "[data-qa^='resume-edit-button-attestationEducation-']",
    "recommendations": "[data-qa^='resume-edit-button-recommendation-']",
}
ATTESTATION_FIELDS = (
    "profile-education-attestation-name",
    "profile-education-attestation-organization",
    "profile-education-attestation-specialty",
    "profile-education-year-input",
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
        "Каждая рекомендация: text, company, name, position."
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
    return page.locator("[data-qa='profile-layout-save-button']")


def _fill_recommendation_row(page: Page, item: Recommendation) -> Locator:
    scope = (
        page.locator("input[name='company']")
        .first.locator("xpath=..")
        .locator("xpath=..")
        .locator("xpath=..")
    )
    _fill(scope.locator("input[name='company']"), item.company)
    inputs = scope.locator("input")
    if inputs.count() > 1:
        _fill(inputs.nth(0), item.name)
    if inputs.count() > 2:
        _fill(inputs.nth(1), item.position)
    _fill(scope.locator("textarea"), item.text)
    return page.locator("[data-qa='resume-partial-edit-save']")


def _apply_rows(
    page: Page,
    block: str,
    items: list[Attestation] | list[Recommendation],
    fill_row,
    *,
    dry_run: bool,
    resume_id: str,
) -> list[str]:
    errors: list[str] = []
    trigger = page.locator(RESUME_EDIT_BUTTON[block])
    for index, item in enumerate(items):
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
            # uncaught and hide which earlier rows already saved.
            if index >= trigger.count():
                errors.append(f"{block}: строка {index} отсутствует; добавление не подтверждено")
                continue
            trigger.nth(index).click()
            page.locator(ready_selector).wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            save = fill_row(page, item)
            if not dry_run:
                if save.count() != 1:
                    errors.append(f"{block}: неоднозначная кнопка сохранения")
                    continue
                save.click()
                page.wait_for_url(f"**/resume/{resume_id}", wait_until="commit")
            else:
                # Leave the row editor before moving to the next row.  Otherwise
                # the next trigger is queried while the previous form is still open.
                cancel_qa = (
                    "resume-partial-edit-cancel"
                    if block == "recommendations"
                    else "profile-layout-cancel-button"
                )
                cancel = page.locator(f"[data-qa='{cancel_qa}']")
                if cancel.count() != 1:
                    errors.append(f"{block}: неоднозначная кнопка отмены")
                    continue
                cancel.click()
        except PlaywrightError as exc:
            # A hydration timeout here may follow an already-successful save.click()
            # on a previous row (#352/codex): fail closed with an explicit error for
            # this row and stop the block instead of letting the exception escape
            # apply_plan and hide which earlier rows already saved.
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
        dry_run=dry_run,
        resume_id=resume_id,
    )
    errors += _apply_rows(
        page,
        "recommendations",
        plan.recommendations,
        _fill_recommendation_row,
        dry_run=dry_run,
        resume_id=resume_id,
    )
    return errors

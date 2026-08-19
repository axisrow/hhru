"""LLM planning and UI editing for primary/additional resume education (#262).

The LLM produces a reviewable plan only. The browser writer never presses Save
in dry-run and uses only selectors confirmed by the authenticated read-only
research in issue #268.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from .browser import (
    goto_hh,
    has_auth_cookie,
    has_login_form,
    open_hydrated_resume_editor,
    resume_identity_matches,
)
from .config_sections.education import EducationRecord
from .responses import NotAuthenticated

logger = logging.getLogger("hhru_bot.resume_education")

PRIMARY_TRIGGER = "[data-qa='resume-edit-button-education-{index}']"
ADDITIONAL_TRIGGER = "[data-qa='resume-edit-button-additionalEducation-{index}']"
# Confirmed by a read-only live DOM probe on the dedicated training resume
# 584926d4ff10f8b2870039ed1f707779623239 (2026-08-18). These links only open
# the form; they do not persist anything until SAVE_BUTTON is clicked.
PRIMARY_ADD = "[data-qa='resume-list-card-education'] [data-qa='link']"
ADDITIONAL_ADD = "[data-qa='resume-list-card-additionalEducation'] [data-qa='link']"
PRIMARY_ROUTE = re.compile(r"/profile/edit/primaryEducation/[^/?#]+")
ADDITIONAL_ROUTE = re.compile(r"/profile/edit/additionalEducation/[^/?#]+")
CANCEL_BUTTON = "[data-qa='profile-layout-cancel-button']"
SAVE_BUTTON = "[data-qa='profile-layout-save-button']"

_PRIMARY_FIELDS = {
    "institution": "[data-qa='profile-education-university-input']",
    "faculty": "[data-qa='profile-education-faculty-input']",
    "specialty": "[data-qa='profile-education-specialty-input']",
    "year": "[data-qa='profile-education-year-input']",
}
_ADDITIONAL_FIELDS = {
    "institution": "[data-qa='profile-education-additional-name']",
    "organization": "[data-qa='profile-education-additional-organization']",
    "specialty": "[data-qa='profile-education-additional-specialty']",
    "year": "[data-qa='profile-education-year-input']",
}
FORM_TIMEOUT_MS = 15_000


@dataclass(frozen=True)
class EducationPlan:
    primary: list[EducationRecord] = field(default_factory=list)
    additional: list[EducationRecord] = field(default_factory=list)
    mode: str = "from_scratch"
    used_fallback: bool = False
    reason: str = ""


def _record(value: Any) -> EducationRecord:
    if not isinstance(value, dict):
        raise ValueError("запись образования должна быть объектом")
    values = {
        k: value.get(k, "")
        for k in ("institution", "level", "faculty", "organization", "specialty", "year")
    }
    if any(not isinstance(v, (str, int)) for v in values.values()):
        raise ValueError("поля образования должны быть строками")
    return EducationRecord(**{k: str(v).strip() for k, v in values.items()})


def _decode(content: str) -> tuple[list[EducationRecord], list[EducationRecord]]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("ответ LLM должен быть JSON-объектом")
    primary = [_record(v) for v in payload.get("primary", [])]
    additional = [_record(v) for v in payload.get("additional", [])]
    if not isinstance(payload.get("primary", []), list) or not isinstance(
        payload.get("additional", []), list
    ):
        raise ValueError("primary и additional должны быть списками")
    return primary, additional


def build_education_prompt(
    source: str,
    *,
    mode: str,
    current_primary: list[EducationRecord] | None = None,
    current_additional: list[EducationRecord] | None = None,
) -> list[dict[str, str]]:
    """Build a strict, non-fabricating prompt for both education blocks."""
    current = {
        "primary": [r.__dict__ for r in current_primary or []],
        "additional": [r.__dict__ for r in current_additional or []],
    }
    system = (
        "Ты заполняешь данные образования кандидата для hh.ru. Верни только JSON без markdown: "
        '{"primary":[{"institution":"","level":"","faculty":"",'
        '"specialty":"","year":""}],'
        '"additional":[{"institution":"","level":"","organization":"",'
        '"specialty":"","year":""}]} . '
        "level — метаданные для плана; в подтвержденной форме hh.ru нет отдельного "
        "поля уровня. Для primary используй faculty, для additional — organization. "
        "Не выдумывай факты; неизвестные поля оставляй пустыми. Сохраняй все учебные заведения."
    )
    user = (
        f"Режим: {mode}. Данные пользователя:\n{source.strip() or '(не указаны)'}\n"
        f"Уже заполнено (дополни, не теряй записи): {json.dumps(current, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def generate_education_plan(
    llm_client,
    source: str,
    *,
    mode: str = "from_scratch",
    current_primary: list[EducationRecord] | None = None,
    current_additional: list[EducationRecord] | None = None,
) -> EducationPlan:
    """Generate a plan; on any AI/shape failure preserve existing data."""
    try:
        response = llm_client.chat(
            build_education_prompt(
                source,
                mode=mode,
                current_primary=current_primary,
                current_additional=current_additional,
            ),
            temperature=0.1,
        )
        content = (response.content if response else "") or ""
        primary, additional = _decode(content)
        return EducationPlan(primary=primary, additional=additional, mode=mode)
    except Exception as exc:  # noqa: BLE001 - feature must fail closed
        logger.warning("AI education generation failed: %s — fallback to supplied values", exc)
        return EducationPlan(
            primary=list(current_primary or []),
            additional=list(current_additional or []),
            mode=mode,
            used_fallback=True,
            reason="AI недоступен или вернул некорректный JSON; сохранён исходный план",
        )


def _edit_block(
    page,
    records: list[EducationRecord],
    *,
    additional: bool,
    dry_run: bool,
    resume_id: str,
) -> EducationResult:
    trigger = ADDITIONAL_TRIGGER if additional else PRIMARY_TRIGGER
    add_selector = ADDITIONAL_ADD if additional else PRIMARY_ADD
    fields = _ADDITIONAL_FIELDS if additional else _PRIMARY_FIELDS
    route = ADDITIONAL_ROUTE if additional else PRIMARY_ROUTE
    kind = "additional" if additional else "primary"
    saved_count = 0
    if not records:
        return EducationResult(kind, True, "нет записей для изменения")
    for index, record in enumerate(records):
        button = page.locator(trigger.format(index=index))
        button_count = button.count()
        if button_count > 1:
            return EducationResult(
                kind,
                False,
                f"триггер образования {index} не найден однозначно",
                uncertain=saved_count > 0,
                saved=saved_count,
            )
        if button_count == 0:
            # The confirmed Add link is the only safe way to create a missing
            # row. Never guess an unverified route or API endpoint.
            button = page.locator(add_selector)
            if button.count() != 1:
                return EducationResult(
                    kind,
                    False,
                    f"строка образования {index} отсутствует, подтвержденная кнопка Добавить "
                    "не найдена однозначно",
                    uncertain=saved_count > 0,
                    saved=saved_count,
                )
        save_clicked = False
        try:
            open_hydrated_resume_editor(
                page,
                trigger_selector=(
                    trigger.format(index=index) if button_count == 1 else add_selector
                ),
                editor_selector=next(iter(fields.values())),
                profile_path=f"/resume/{resume_id}",
                edit_path=route,
                timeout=FORM_TIMEOUT_MS,
                trigger_error=f"триггер образования {index} не найден однозначно",
                open_error=f"форма образования {index} не открылась",
                wrong_route_error=f"форма образования {index} открыта не для того резюме",
            )
            for name, selector in fields.items():
                value = getattr(record, name)
                # Empty LLM fields mean "unknown", not "erase the current value".
                # This protects prefill and also makes a partial from-scratch plan
                # fail closed rather than destroy data already on hh.ru.
                if not value:
                    continue
                locator = page.locator(selector)
                if locator.count() != 1:
                    return EducationResult(
                        kind,
                        False,
                        f"поле {selector} не найдено однозначно",
                        uncertain=saved_count > 0,
                        saved=saved_count,
                    )
                locator.fill(value)
            if dry_run:
                page.locator(CANCEL_BUTTON).first.click()
            else:
                save = page.locator(SAVE_BUTTON)
                if save.count() != 1:
                    return EducationResult(
                        kind,
                        False,
                        "кнопка сохранения не найдена однозначно",
                        uncertain=saved_count > 0,
                        saved=saved_count,
                    )
                save_clicked = True
                save.click()
                page.wait_for_url(f"**/resume/{resume_id}", wait_until="commit")
                if not resume_identity_matches(page, resume_id):
                    return EducationResult(
                        kind,
                        False,
                        "после сохранения identity резюме не подтверждён",
                        uncertain=True,
                        saved=saved_count,
                    )
                saved_count += 1
        except (PlaywrightError, RuntimeError) as exc:
            # open_hydrated_resume_editor raises RuntimeError (not PlaywrightError)
            # for trigger-not-found/open-failed/wrong-route — a hydration failure
            # on a later row must not escape _edit_block uncaught after an earlier
            # row already saved (#352/#331 pattern, codex round 1 finding on #368).
            return EducationResult(
                kind,
                False,
                f"ошибка UI: {exc}",
                uncertain=save_clicked or saved_count > 0,
                saved=saved_count,
            )
    return EducationResult(
        kind,
        True,
        f"обработано записей: {len(records)}"
        + ("; save не нажимался" if dry_run else "; сохранение подтверждено возвратом на резюме"),
        saved=saved_count,
    )


@dataclass(frozen=True)
class EducationResult:
    kind: str
    success: bool
    reason: str
    uncertain: bool = False
    saved: int = 0


def edit_education_on_hh(
    page, resume_url: str, plan: EducationPlan, *, section: str = "both", dry_run: bool = True
) -> list[EducationResult]:
    """Apply selected blocks through the confirmed UI flow; never HTTP."""
    if not has_auth_cookie(page) or has_login_form(page):
        raise NotAuthenticated("сессия hh.ru не подтверждена")
    path_parts = [part for part in urlsplit(resume_url).path.split("/") if part]
    if len(path_parts) != 2 or path_parts[0] != "resume" or not path_parts[1]:
        raise ValueError("resume_url не содержит однозначный resume_id")
    resume_id = path_parts[1]
    goto_hh(page, resume_url)
    results = []
    if section in ("primary", "both"):
        results.append(
            _edit_block(page, plan.primary, additional=False, dry_run=dry_run, resume_id=resume_id)
        )
    if section in ("additional", "both"):
        results.append(
            _edit_block(
                page, plan.additional, additional=True, dry_run=dry_run, resume_id=resume_id
            )
        )
    return results

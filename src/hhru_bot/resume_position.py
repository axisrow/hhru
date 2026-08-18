"""LLM planning and browser editing for the resume position block (#259).

The browser side deliberately uses only selectors confirmed by the live probe in
issue #268.  In particular, editing is inline on ``/resume/<id>``; there is no
``/edit`` route.  The save button is kept behind the command's explicit write
confirmation and is never clicked by the dry-run path.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from playwright.sync_api import Page

from .browser import HH_BASE_URL, goto_hh, has_login_form
from .config import ResumeConfig
from .responses import NotAuthenticated

FORM = "[data-qa='resume-edit-position-form']"
EDIT = "[data-qa='edit-position-button']"
TITLE = "[data-qa='resume-edit-title-suggest']"
SALARY = "[data-qa='resume-salary-amount']"
CURRENCY = {code: f"[data-qa='resume-currency-input-{code}']" for code in ("RUR", "EUR", "USD")}
EMPLOYMENT = "[data-qa='resume-edit-employment-forms']"
WORK_FORMAT = "[data-qa='resume-edit-work-formats']"
TRAVEL = "[data-qa='resume-edit-travel-time']"
BUSINESS_TRIPS = "[data-qa='resume-edit-business-trip-readiness']"
CANCEL = "[data-qa='resume-partial-edit-cancel']"
SAVE = "[data-qa='resume-partial-edit-save']"

EMPLOYMENT_LABELS = {
    "full_time": "Полная занятость",
    "part_time": "Частичная занятость",
    "project": "Проектная работа",
    "internship": "Стажировка",
    "volunteer": "Волонтёрство",
}
WORK_LABELS = {"office": "Офис", "hybrid": "Гибрид", "remote": "Удалённо"}
TRAVEL_LABELS = {
    "no_limit": "Не имеет значения",
    "up_to_1_hour": "Не дольше 1 часа",
    "up_to_2_hours": "Не дольше 2 часов",
    "up_to_3_hours": "Не дольше 3 часов",
}
DISPLAY_EMPLOYMENT = {**EMPLOYMENT_LABELS, "full_time": "Постоянная работа"}
DISPLAY_WORK = {**WORK_LABELS, "office": "На месте работодателя", "remote": "Удалённо"}


@dataclass
class PositionValues:
    title: str = ""
    salary: int | None = None
    currency: str | None = None
    specializations: list[str] | None = None
    employment: list[str] | None = None
    work_format: list[str] | None = None
    commute: str | None = None
    business_trips: bool | None = None


def _profile_context(profile: Any) -> dict[str, Any]:
    if profile is None:
        return {}
    return {
        "summary": getattr(profile, "summary", ""),
        "skills": list(getattr(profile, "skills", [])),
        "highlights": list(getattr(profile, "highlights", [])),
        "desired_role": getattr(profile, "desired_role", ""),
    }


def build_position_prompt(profile: Any, current: PositionValues, mode: str) -> list[dict[str, str]]:
    """Build a JSON-only prompt; salary must be copied from facts or remain null."""
    system = (
        "Ты заполняешь структурированный раздел желаемой работы в резюме. "
        "Ответь только JSON без markdown с ключами title, salary, currency, "
        "specializations, employment, work_format, commute, business_trips. "
        "employment и work_format — массивы enum. Допустимые employment: "
        "full_time, part_time, project, internship, volunteer. Допустимые "
        "work_format: office, hybrid, remote. Допустимые commute: no_limit, "
        "up_to_1_hour, up_to_2_hours, up_to_3_hours. salary — целое число или null. "
        "Никогда не выдумывай salary, currency или условия. В режиме fill не меняй "
        "уже заполненные значения; specializations оставь пустым, если фактов нет."
    )
    payload = {"mode": mode, "candidate": _profile_context(profile), "current": asdict(current)}
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_position_response(content: str | None) -> PositionValues:
    if not content or not content.strip():
        raise ValueError("LLM вернул пустой план")
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM-план должен быть JSON-объектом")
    salary = data.get("salary")
    if salary is not None and (
        isinstance(salary, bool) or not isinstance(salary, int) or salary < 0
    ):
        raise ValueError("salary должен быть неотрицательным целым числом или null")
    currency = data.get("currency")
    if currency is not None and currency not in CURRENCY:
        raise ValueError("currency должен быть RUR, EUR, USD или null")

    def strings(key: str) -> list[str] | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"{key} должен быть списком строк")
        return value

    employment = strings("employment")
    work = strings("work_format")
    if employment and not set(employment) <= set(EMPLOYMENT_LABELS):
        raise ValueError("LLM вернул недопустимый employment enum")
    if work and not set(work) <= set(WORK_LABELS):
        raise ValueError("LLM вернул недопустимый work_format enum")
    commute = data.get("commute")
    if commute is not None and commute not in TRAVEL_LABELS:
        raise ValueError("LLM вернул недопустимый commute enum")
    trips = data.get("business_trips")
    if trips is not None and not isinstance(trips, bool):
        raise ValueError("business_trips должен быть boolean или null")
    return PositionValues(
        title=str(data.get("title") or "").strip(),
        salary=salary,
        currency=currency,
        specializations=strings("specializations"),
        employment=employment,
        work_format=work,
        commute=commute,
        business_trips=trips,
    )


def _value(locator) -> str:
    if not locator.count():
        return ""
    return (
        locator.first.input_value()
        if locator.first.evaluate("e=>['INPUT','TEXTAREA'].includes(e.tagName)")
        else locator.first.inner_text()
    ).strip()


def read_position(page: Page) -> PositionValues:
    """Read the confirmed inline form after opening it; no save or mutation."""
    title = _value(page.locator(TITLE))
    salary_text = _value(page.locator(SALARY))
    try:
        salary = int(salary_text) if salary_text else None
    except ValueError:
        salary = None
    return PositionValues(title=title, salary=salary)


def read_display_position(page: Page) -> PositionValues:
    """Read the already published values before opening the inline editor."""

    def text(selector: str) -> str:
        loc = page.locator(selector)
        return (loc.first.inner_text() if loc.count() == 1 else "").strip()

    title = text("[data-qa='resume-block-title-position']")
    salary_text = text("[data-qa='resume-block-salary']")
    digits = re.sub(r"[^0-9]", "", salary_text)
    salary = int(digits) if digits else None
    currency = next(
        (
            code
            for code, symbol in (("RUR", "₽"), ("EUR", "€"), ("USD", "$"))
            if symbol in salary_text
        ),
        None,
    )
    employment_text = text("[data-qa='resume-position-field-employmentForms']")
    employment = next(
        (key for key, label in DISPLAY_EMPLOYMENT.items() if label in employment_text), None
    )
    work_text = text("[data-qa='resume-position-field-workFormats']")
    work_format = next((key for key, label in DISPLAY_WORK.items() if label in work_text), None)
    commute_text = text("[data-qa='resume-position-field-travelTime']")
    commute = next((key for key, label in TRAVEL_LABELS.items() if label in commute_text), None)
    trips_text = text("[data-qa='resume-position-field-businessTripReadiness']")
    trips = (
        True
        if "Могу" in trips_text and "Не могу" not in trips_text
        else False
        if "Не могу" in trips_text
        else None
    )
    return PositionValues(
        title=title,
        salary=salary,
        currency=currency,
        employment=[employment] if employment else None,
        work_format=[work_format] if work_format else None,
        commute=commute,
        business_trips=trips,
    )


def fill_only_missing(current: PositionValues, plan: PositionValues) -> PositionValues:
    """Apply the fill-mode invariant outside the model: existing values win."""
    return PositionValues(
        title="" if current.title else plan.title,
        salary=None if current.salary is not None else plan.salary,
        currency=None if current.currency is not None else plan.currency,
        specializations=plan.specializations,
        employment=None if current.employment else plan.employment,
        work_format=None if current.work_format else plan.work_format,
        commute=None if current.commute else plan.commute,
        business_trips=None if current.business_trips is not None else plan.business_trips,
    )


def open_position_form(page: Page, resume: ResumeConfig) -> PositionValues:
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume.resume_id}")
    if has_login_form(page):
        raise NotAuthenticated("страница содержит форму входа — сессия отвергнута")
    current = read_display_position(page)
    if page.locator(FORM).count() == 0:
        if page.locator(EDIT).count() != 1:
            raise RuntimeError("кнопка редактирования позиции не подтверждена")
        page.locator(EDIT).click()
        page.locator(FORM).wait_for(state="visible", timeout=10_000)
    form_values = read_position(page)
    return PositionValues(
        title=form_values.title or current.title,
        salary=form_values.salary if form_values.salary is not None else current.salary,
        currency=form_values.currency or current.currency,
        employment=form_values.employment or current.employment,
        work_format=form_values.work_format or current.work_format,
        commute=form_values.commute or current.commute,
        business_trips=form_values.business_trips
        if form_values.business_trips is not None
        else current.business_trips,
    )


def _set_control(page: Page, selector: str, value: str, labels: dict[str, str]) -> None:
    loc = page.locator(selector)
    if loc.count() != 1:
        raise RuntimeError(f"селектор формы не подтверждён: {selector}")
    el = loc.first
    tag = el.evaluate("e=>e.tagName")
    if tag in ("INPUT", "TEXTAREA"):
        el.fill(value)
        return
    el.click()
    option = page.get_by_role("option", name=labels[value], exact=True)
    if option.count() != 1:
        raise RuntimeError(f"вариант формы не найден: {labels[value]}")
    option.click()


def apply_position(page: Page, plan: PositionValues) -> None:
    """Fill fields only. Caller owns confirmation and must click SAVE explicitly."""
    if plan.specializations:
        raise RuntimeError("селектор specializations не подтверждён на форме hh.ru")
    if plan.title:
        page.locator(TITLE).fill(plan.title)
    if plan.salary is not None:
        page.locator(SALARY).fill(str(plan.salary))
    if plan.currency is not None:
        currency = page.locator(CURRENCY[plan.currency])
        if currency.count() != 1:
            raise RuntimeError(f"селектор валюты не подтверждён: {plan.currency}")
        currency.click()
    if plan.employment:
        for value in plan.employment:
            _set_control(page, EMPLOYMENT, value, EMPLOYMENT_LABELS)
    if plan.work_format:
        for value in plan.work_format:
            _set_control(page, WORK_FORMAT, value, WORK_LABELS)
    if plan.commute:
        _set_control(page, TRAVEL, plan.commute, TRAVEL_LABELS)
    if plan.business_trips is not None:
        _set_control(
            page,
            BUSINESS_TRIPS,
            "true" if plan.business_trips else "false",
            {"true": "Могу", "false": "Не могу"},
        )

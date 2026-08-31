"""LLM planning and browser editing for the resume position block (#259).

The browser side deliberately uses only selectors confirmed by the live probe.
Opening the position editor navigates to its dedicated ``/resume/edit/<id>/position``
route, so its form must not be queried until that navigation has committed (#328).
The save button is kept behind the command's explicit write confirmation and is
never clicked by the dry-run path.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser import (
    HH_BASE_URL,
    goto_hh,
    open_hydrated_resume_editor,
    require_authenticated_page,
    resume_identity_matches,
)
from .config import ResumeConfig
from .logging_setup import LOG_DIR
from .resume_state import ResumeState, parse_resume_state
from .selector_groups.resume_page import (
    RESUME_CREATION_CATEGORY_INPUT as WIZARD_CATEGORY_INPUT,
)
from .selector_groups.resume_page import (
    RESUME_CREATION_CATEGORY_SEARCH as WIZARD_CATEGORY_SEARCH,
)
from .selector_groups.resume_page import (
    RESUME_CREATION_CATEGORY_SUBMIT as WIZARD_CATEGORY_SUBMIT,
)
from .selector_groups.resume_page import RESUME_CREATION_NEXT as WIZARD_NEXT
from .selector_groups.resume_page import RESUME_CREATION_POSITION as WIZARD_POSITION
from .selector_groups.resume_page import (
    RESUME_CREATION_POSITION_CHIP_POPULAR as WIZARD_POSITION_CHIP_POPULAR_BASE,
)
from .selector_groups.resume_page import (
    RESUME_CREATION_POSITION_CLEAR as WIZARD_POSITION_CLEAR,
)
from .selector_groups.resume_page import (
    RESUME_CREATION_SELECT_JOB as WIZARD_SELECT_JOB,
)
from .selector_groups.resume_page import RESUME_CREATION_URL as WIZARD_PATH
from .selector_groups.resume_page import RESUME_POSITION_DROPDOWN
from .selector_groups.resume_page import (
    RESUME_SPECIALIZATION_ADD as SPECIALIZATION_ADD,
)
from .selector_groups.resume_page import (
    RESUME_SPECIALIZATION_DELETE as SPECIALIZATION_DELETE,
)
from .selector_groups.resume_page import (
    RESUME_SPECIALIZATION_MODAL as SPECIALIZATION_MODAL,
)
from .selector_groups.resume_page import (
    RESUME_SPECIALIZATION_OPTION as SPECIALIZATION_OPTION,
)
from .selector_groups.resume_page import (
    RESUME_SPECIALIZATION_SEARCH as SPECIALIZATION_SEARCH,
)
from .selector_groups.resume_page import (
    RESUME_SPECIALIZATION_SUBMIT as SPECIALIZATION_SUBMIT,
)

logger = logging.getLogger("hhru_bot.resume_position")


class WizardRoleMismatch(RuntimeError):
    """Post-save readback found a different professional role.

    This is the one verification failure for which the legacy minimum-wizard
    fallback remains appropriate; transport/state failures stay fail-closed.
    """


class ChipPopularUnavailable(RuntimeError):
    """The chip-popular shape (#881/#889) cannot confirm the exact catalog
    specialization — it is a fixed list of ~37 generic categories plus a
    narrow "Уточните профессию" sub-modal (~15 items each) that does not
    contain most real catalog leaves (confirmed live DOM 2026-08-31: role_id
    96 "Программист, разработчик" is absent from the "Программист" sub-list).
    A dedicated exception type (not a plain RuntimeError) lets the caller
    distinguish "this shape cannot do it, try the wizard-minimum fallback"
    from every other wizard failure, which must still surface as-is.
    """


# The chip-popular radio does not carry the profession in its data-qa (every
# chip on the screen shares the same data-qa); the profession lives in the
# input's ``value`` attribute instead (#881, live DOM 2026-08-31). Scope the
# base selector down to the one radio matching the confirmed catalog label.
WIZARD_POSITION_CHIP_POPULAR = WIZARD_POSITION_CHIP_POPULAR_BASE + "[value='{}']"

# Fixed, deterministic placeholder for the wizard-minimum fallback (#890):
# any of the ~37 chip-popular categories clears `nextIncompleteScreenId`
# equally well, since its content is discarded immediately by the editor-mode
# `_set_specializations` call that follows. A hardcoded, always-identical
# choice (first item on the confirmed live list) is deliberate — not derived
# from `plan.title` or randomized — per the project's anti-fraud principle of
# not behaving unpredictably towards hh.ru (CLAUDE.md).
WIZARD_MINIMUM_PLACEHOLDER_TITLE = "Администратор"

# Explicit, generous but bounded — avoids a silent 30s-default hang per call
# (CLAUDE.md requires an inline timeout with a comment for every post-render
# wait; the position editor has none of its own dedicated helper).
_CONTROL_WAIT_TIMEOUT_MS = 5_000

FORM = "[data-qa='resume-edit-position-form']"
EDIT = "[data-qa='edit-position-button']"
TITLE = "[data-qa='resume-edit-title-suggest']"
SALARY = "[data-qa='resume-salary-amount']"
CURRENCY = {code: f"[data-qa='resume-currency-input-{code}']" for code in ("RUR", "EUR", "USD")}
CURRENCY_LABELS = {"RUR": "Рубли", "EUR": "Евро", "USD": "Доллары"}
EMPLOYMENT = "[data-qa='resume-edit-employment-forms']"
WORK_FORMAT = "[data-qa='resume-edit-work-formats']"
TRAVEL = "[data-qa='resume-edit-travel-time']"
BUSINESS_TRIPS = "[data-qa='resume-edit-business-trip-readiness']"
CANCEL = "[data-qa='resume-partial-edit-cancel']"
SAVE = "[data-qa='resume-partial-edit-save']"
WIZARD_WAIT_MS = 15_000
WIZARD_TRANSITION_ATTEMPTS = 15
WIZARD_TRANSITION_POLL_MS = 1_000
WIZARD_VERIFY_TIMEOUT_MS = 30_000
WIZARD_VERIFY_POLL_MS = 500

# Панель "Тип занятости" на hh.ru показывает ровно эти 4 опции (confirmed live
# DOM, #799): нет отдельных "Частичная занятость"/"Проектная работа" — hh.ru
# объединил их в общую панель, а "full_time" отображается и кликается как
# "Постоянная работа", не "Полная занятость". Единый словарь используется и
# для клика по опции (_set_control), и для read_display_position — расхождение
# между ними (#799) было первопричиной "вариант формы не найден".
EMPLOYMENT_LABELS = {
    "full_time": "Постоянная работа",
    "part_time": "Подработка",
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
DISPLAY_EMPLOYMENT = EMPLOYMENT_LABELS
DISPLAY_WORK = {**WORK_LABELS, "office": "На месте работодателя", "remote": "Удалённо"}


@dataclass
class PositionValues:
    # ``None`` means leave the title unchanged; an empty string is an explicit
    # request to clear it (needed by baseline/restore flows).
    title: str | None = None
    salary: int | None = None
    currency: str | None = None
    specializations: list[str] | None = None
    employment: list[str] | None = None
    work_format: list[str] | None = None
    commute: str | None = None
    business_trips: bool | None = None


@dataclass(frozen=True)
class PositionFlowContext:
    """Identity-bound editor selection derived from server state, not URL luck."""

    kind: Literal["wizard", "editor"]
    resume_id: str
    values: PositionValues
    state: ResumeState


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
        "full_time, part_time, internship, volunteer. Допустимые "
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
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError("title должен быть строкой или null")
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
        title=title.strip() if title is not None else None,
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
        title=None if current.title else plan.title,
        salary=None if current.salary is not None else plan.salary,
        currency=None if current.currency is not None else plan.currency,
        specializations=plan.specializations,
        employment=None if current.employment else plan.employment,
        work_format=None if current.work_format else plan.work_format,
        commute=None if current.commute else plan.commute,
        business_trips=None if current.business_trips is not None else plan.business_trips,
    )


def _open_position_wizard(
    page: Page,
    resume: ResumeConfig,
    *,
    state: ResumeState,
) -> PositionFlowContext:
    if not is_position_wizard(page, resume.resume_id):
        query = urlencode({"resume": resume.resume_id})
        goto_hh(page, f"{HH_BASE_URL}{WIZARD_PATH}?{query}")
        require_authenticated_page(page)
    if not is_position_wizard(page, resume.resume_id):
        raise RuntimeError("визард professional_role открыт не для того резюме")

    position = _advance_wizard_to_position(page, resume)
    if position.count() != 1:
        raise RuntimeError(f"поле должности визарда неоднозначно: {position.count()}")
    if logger.isEnabledFor(logging.DEBUG):
        _dump_wizard_failure(page, resume.resume_id, "wizard_open")
    return PositionFlowContext(
        kind="wizard",
        resume_id=resume.resume_id,
        values=PositionValues(title=_value(position)),
        state=state,
    )


def _advance_wizard_to_position(page: Page, resume: ResumeConfig):
    """Cross the SSR-to-hydrated card transition, with one safe reload.

    The entry card can become visible from SSR before React attaches its click
    handler. A click that leaves the exact same card visible is not progress.
    Retrying this navigation-only card is safe: no resume write happens before
    the final controls in :func:`save_position_wizard`.
    """
    for load_attempt in range(2):
        position = page.locator(WIZARD_POSITION)
        position_count = position.count()
        if position_count == 1:
            position.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
            return position
        if position_count > 1:
            raise RuntimeError(f"поле должности визарда неоднозначно: {position_count}")

        select_job = page.locator(WIZARD_SELECT_JOB)
        try:
            select_job.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
        except PlaywrightError as exc:
            raise RuntimeError(f"визард professional_role не отрисовался: {exc}") from exc
        if select_job.count() != 1:
            raise RuntimeError(f"карточка выбора профессии неоднозначна: {select_job.count()}")

        for _ in range(WIZARD_TRANSITION_ATTEMPTS):
            if position.count() == 1:
                position.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
                return position
            if select_job.count() != 1:
                break
            try:
                select_job.first.click(timeout=WIZARD_TRANSITION_POLL_MS)
            except PlaywrightError:
                # A visible SSR card may not be actionable until hydration.
                pass
            page.wait_for_timeout(WIZARD_TRANSITION_POLL_MS)

        if load_attempt == 0 and select_job.count() == 1:
            page.reload(wait_until="domcontentloaded")
            require_authenticated_page(page)
            if not is_position_wizard(page, resume.resume_id):
                raise RuntimeError("professional_role identity потерян после recovery reload")
            continue
        break

    dump = _dump_wizard_failure(page, resume.resume_id, "position_missing")
    raise RuntimeError(f"поле должности визарда не появилось; url={page.url}; диагностика={dump}")


def _dump_wizard_failure(page: Page, resume_id: str, reason: str) -> str:
    """Persist authenticated DOM evidence after a read-only wizard failure."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"professional_role_{resume_id}_{reason}"
    html_path = LOG_DIR / f"{stem}.html"
    screenshot_path = LOG_DIR / f"{stem}.png"
    try:
        html_path.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(screenshot_path), full_page=True)
    except PlaywrightError as exc:
        logger.warning("professional_role: диагностический дамп неполон: %s", exc)
    return str(html_path)


def open_position_form(page: Page, resume: ResumeConfig) -> PositionFlowContext:
    """Open the state-selected position flow for exactly one resume.

    hh.ru does not consistently redirect unfinished drafts from ``/resume/<id>``
    to the professional-role wizard.  The embedded, identity-bound state is the
    source of truth; the URL only proves which surface was opened.
    """
    goto_hh(page, f"{HH_BASE_URL}/resume/{resume.resume_id}")
    require_authenticated_page(page)
    if _is_wizard_path(getattr(page, "url", "")):
        return _open_position_wizard(
            page,
            resume,
            state=ResumeState(next_incomplete_screen_id="professional_role"),
        )
    if not resume_identity_matches(page, resume.resume_id):
        raise RuntimeError("страница позиции открыта не для того резюме")

    state = parse_resume_state(page.content(), resume.resume_id)
    if state.status is None:
        raise RuntimeError("состояние резюме не подтверждено перед выбором position flow")
    if state.next_incomplete_screen_id == "professional_role":
        return _open_position_wizard(page, resume, state=state)

    current = read_display_position(page)
    edit_path = f"/resume/edit/{resume.resume_id}/position"
    open_hydrated_resume_editor(
        page,
        trigger_selector=EDIT,
        editor_selector=FORM,
        profile_path=f"/resume/{resume.resume_id}",
        edit_path=edit_path,
        trigger_error="кнопка редактирования позиции не подтверждена",
        open_error="форма редактирования позиции не открылась",
        wrong_route_error="форма редактирования позиции открыта не для того резюме",
    )
    form_values = read_position(page)
    if logger.isEnabledFor(logging.DEBUG):
        _dump_wizard_failure(page, resume.resume_id, "editor_open")
    return PositionFlowContext(
        kind="editor",
        resume_id=resume.resume_id,
        state=state,
        values=PositionValues(
            title=form_values.title or current.title,
            salary=form_values.salary if form_values.salary is not None else current.salary,
            currency=form_values.currency or current.currency,
            employment=form_values.employment or current.employment,
            work_format=form_values.work_format or current.work_format,
            commute=form_values.commute or current.commute,
            business_trips=form_values.business_trips
            if form_values.business_trips is not None
            else current.business_trips,
        ),
    )


def _is_wizard_path(url: object) -> bool:
    return isinstance(url, str) and urlsplit(url).path == WIZARD_PATH


def is_position_wizard(page: Page, resume_id: str) -> bool:
    """Return true only for the requested draft's professional-role wizard."""
    url = getattr(page, "url", "")
    if not _is_wizard_path(url):
        return False
    query = parse_qs(urlsplit(url).query)
    return query.get("resume") == [resume_id]


def validate_wizard_plan(plan: PositionValues) -> None:
    """Reject fields the first draft wizard cannot save without dropping data."""
    if plan.title == "":
        raise ValueError("Пустой title отклоняется hh.ru")
    if not plan.title:
        raise RuntimeError("для professional_role требуется непустой --title")
    if not plan.specializations or len(plan.specializations) != 1:
        raise RuntimeError("для professional_role требуется ровно одна профессия каталога")
    unsupported = {
        "salary": plan.salary,
        "currency": plan.currency,
        "employment": plan.employment,
        "work_format": plan.work_format,
        "commute": plan.commute,
        "business_trips": plan.business_trips,
    }
    supplied = [name for name, value in unsupported.items() if value not in (None, [])]
    if supplied:
        raise RuntimeError(
            "визард professional_role не сохраняет остальные поля за один шаг: "
            + ", ".join(supplied)
        )


def save_position_wizard(
    page: Page,
    resume: ResumeConfig,
    plan: PositionValues,
    *,
    role_id: str,
    before_first_click: Callable[[], None] | None = None,
) -> None:
    """Save one title/role pair in the draft wizard after caller confirmation."""
    validate_wizard_plan(plan)
    if not is_position_wizard(page, resume.resume_id):
        raise RuntimeError("professional_role identity не подтверждён перед сохранением")

    position = page.locator(WIZARD_POSITION)
    if position.count() != 1:
        raise RuntimeError(f"поле должности визарда неоднозначно: {position.count()}")
    clear = page.locator(WIZARD_POSITION_CLEAR)
    clear_count = clear.count()
    if clear_count > 1:
        raise RuntimeError(f"очистка должности визарда неоднозначна: {clear.count()}")
    if clear_count == 1:
        clear.click()
        clear_deadline = time.monotonic() + WIZARD_WAIT_MS / 1000
        while time.monotonic() < clear_deadline and position.input_value():
            page.wait_for_timeout(WIZARD_VERIFY_POLL_MS)
    if position.input_value():
        raise RuntimeError("визард не подтвердил очистку прежней должности и profession IDs")

    position.fill(plan.title or "")
    next_button = page.locator(WIZARD_NEXT)
    try:
        next_button.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
    except PlaywrightError as exc:
        raise RuntimeError(f"кнопка продолжения визарда не появилась: {exc}") from exc
    if next_button.count() != 1:
        raise RuntimeError(f"кнопка продолжения визарда неоднозначна: {next_button.count()}")
    if before_first_click is not None:
        before_first_click()
    next_button.click()

    expected_label = plan.specializations[0]
    search = page.locator(WIZARD_CATEGORY_SEARCH)
    # The chip's ``value`` mirrors the just-typed title, not the catalog
    # specialization label confirmed below for the modal path (#881, live DOM
    # 2026-08-31) — hh.ru pre-fills the chip from what the user entered above.
    chip = page.locator(WIZARD_POSITION_CHIP_POPULAR.format(plan.title))
    transition_deadline = time.monotonic() + WIZARD_WAIT_MS / 1000
    while time.monotonic() < transition_deadline:
        if not _is_wizard_path(getattr(page, "url", "")):
            return
        if search.count() == 1:
            search.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
            break
        if chip.count() == 1:
            # Second post-NEXT shape (#881/#889, live DOM 2026-08-31): hh.ru
            # skips the full tree-selector catalog modal and instead shows a
            # fixed list of ~37 generic categories, each opening a narrow
            # "Уточните профессию" sub-modal with ~15 items. This shape
            # cannot reach an arbitrary catalog leaf: live DOM confirmed
            # role_id 96 "Программист, разработчик" is absent from the
            # sub-modal under its own general category "Программист" — the
            # chip's checked state only reflects the just-typed title, never
            # the requested ``expected_label``. Clicking the chip and NEXT
            # here would either fail closed (unchecked/disabled) or silently
            # save the WRONG specialization (checked+enabled). Neither
            # outcome can satisfy ``expected_label``, so this shape is
            # unusable for an exact save and the caller must fall back to
            # the wizard-minimum + editor path instead of attempting it.
            raise ChipPopularUnavailable(
                f"chip-popular shape не может сохранить точную специализацию "
                f"«{expected_label}» — каталог этого экрана её не содержит"
            )
        page.wait_for_timeout(WIZARD_VERIFY_POLL_MS)
    else:
        raise RuntimeError("каталог профессий не появился после очистки прежних profession IDs")
    if search.count() != 1:
        raise RuntimeError(f"поиск профессий визарда неоднозначен: {search.count()}")
    search.fill(expected_label)
    checkbox = page.locator(WIZARD_CATEGORY_INPUT.format(role_id))
    checkbox.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
    checkbox_count = checkbox.count()
    if checkbox_count == 0:
        raise RuntimeError(f"профессия «{expected_label}» не подтверждена по role_id={role_id}")
    for index in range(checkbox_count):
        row = checkbox.nth(index).locator("xpath=ancestor::label[1]")
        text = row.locator("[data-qa='cell-text-content']")
        if (
            row.count() != 1
            or text.count() != 1
            or (text.first.inner_text() or "").strip() != expected_label
        ):
            raise RuntimeError("label профессии не совпал с согласованным live-каталогом")
    checkbox.first.check()
    submit = page.locator(WIZARD_CATEGORY_SUBMIT)
    if submit.count() != 1:
        raise RuntimeError(f"кнопка подтверждения каталога неоднозначна: {submit.count()}")
    submit.click()

    # Current hh.ru builds may commit the selected role from the modal itself;
    # older wizard builds only close the modal and require the final NEXT.  In
    # the latter case, waiting for a route change before clicking NEXT would
    # turn a valid save into an unnecessary uncertain timeout.
    if _is_wizard_path(getattr(page, "url", "")):
        next_button = page.locator(WIZARD_NEXT)
        try:
            next_button.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
        except PlaywrightError as exc:
            raise RuntimeError(f"финальная кнопка продолжения визарда не появилась: {exc}") from exc
        if next_button.count() != 1:
            raise RuntimeError(
                f"финальная кнопка продолжения визарда неоднозначна: {next_button.count()}"
            )
        next_button.click()
    page.wait_for_url(
        lambda url: urlsplit(str(url)).path != WIZARD_PATH,
        wait_until="commit",
        timeout=30_000,
    )


def save_position_wizard_minimum(
    page: Page,
    resume: ResumeConfig,
    *,
    before_first_click: Callable[[], None] | None = None,
) -> str:
    """Save ANY valid chip-popular category to clear professional_role (#890).

    This is the fallback for :func:`save_position_wizard` raising
    :class:`ChipPopularUnavailable`: the chip-popular shape cannot reach an
    arbitrary catalog leaf, but the caller does not need it to — it only
    needs `nextIncompleteScreenId` to stop being `"professional_role"` so
    :func:`open_position_form` returns ``kind="editor"`` next time, where the
    exact specialization is set through the already-working
    ``apply_position``/``_set_specializations`` catalog search. The saved
    title is deliberately the fixed :data:`WIZARD_MINIMUM_PLACEHOLDER_TITLE`,
    not ``plan.title`` — its content is thrown away immediately by the editor
    step that follows, so there is nothing to get right here beyond a valid,
    checked, non-disabled chip.

    Returns the placeholder title actually saved, for the caller to log/pass
    to the editor step's ``current`` baseline.
    """
    if not is_position_wizard(page, resume.resume_id):
        raise RuntimeError("professional_role identity не подтверждён перед сохранением")

    title = WIZARD_MINIMUM_PLACEHOLDER_TITLE
    position = page.locator(WIZARD_POSITION)
    if position.count() != 1:
        raise RuntimeError(f"поле должности визарда неоднозначно: {position.count()}")
    clear = page.locator(WIZARD_POSITION_CLEAR)
    clear_count = clear.count()
    if clear_count > 1:
        raise RuntimeError(f"очистка должности визарда неоднозначна: {clear_count}")
    if clear_count == 1:
        clear.click()
        clear_deadline = time.monotonic() + WIZARD_WAIT_MS / 1000
        while time.monotonic() < clear_deadline and position.input_value():
            page.wait_for_timeout(WIZARD_VERIFY_POLL_MS)
    if position.input_value():
        raise RuntimeError("визард не подтвердил очистку прежней должности и profession IDs")

    position.fill(title)
    next_button = page.locator(WIZARD_NEXT)
    try:
        next_button.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
    except PlaywrightError as exc:
        raise RuntimeError(f"кнопка продолжения визарда не появилась: {exc}") from exc
    if next_button.count() != 1:
        raise RuntimeError(f"кнопка продолжения визарда неоднозначна: {next_button.count()}")
    if before_first_click is not None:
        before_first_click()
    next_button.click()

    chip = page.locator(WIZARD_POSITION_CHIP_POPULAR.format(title))
    deadline = time.monotonic() + WIZARD_WAIT_MS / 1000
    while time.monotonic() < deadline:
        if not _is_wizard_path(getattr(page, "url", "")):
            return title
        if chip.count() == 1:
            break
        page.wait_for_timeout(WIZARD_VERIFY_POLL_MS)
    else:
        raise RuntimeError(
            f"chip-popular для «{title}» не появился — минимальное сохранение невозможно"
        )
    chip.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
    if not chip.first.is_checked():
        raise RuntimeError(f"чип должности «{title}» найден, но не отмечен")
    if chip.first.is_disabled():
        raise RuntimeError(f"чип должности «{title}» отмечен, но отключён")
    # The radio input itself is disabled in this shape; its wrapping card is
    # the actual hit target (same pattern as the currency-chip click in
    # ``apply_position`` below).
    chip_card = chip.first.locator("xpath=ancestor::label[1]")
    if chip_card.count() != 1:
        raise RuntimeError("карточка chip должности не подтверждена")
    chip_card.click()
    next_button.first.wait_for(state="visible", timeout=WIZARD_WAIT_MS)
    try:
        next_button.click()
    except PlaywrightError:
        # The route change itself is the positive signal; only re-raise if
        # we are still stuck on professional_role.
        if not _is_wizard_path(getattr(page, "url", "")):
            return title
        raise
    try:
        page.wait_for_url(
            lambda url: urlsplit(str(url)).path != WIZARD_PATH,
            wait_until="commit",
            timeout=30_000,
        )
    except PlaywrightError:
        _dump_wizard_failure(page, resume.resume_id, "wizard_minimum_post_next")
        raise
    return title


def verify_wizard_minimum_save(page: Page, resume: ResumeConfig) -> ResumeState:
    """Poll only for professional_role clearing — no title/role match (#890).

    Deliberately narrower than :func:`verify_wizard_save`: the wizard-minimum
    step saves a throwaway placeholder on purpose, so checking its title or
    role against anything would be a foot-gun (a future call could weaken
    real verification by passing loose expectations). This function proves
    exactly one fact — the draft left the professional_role screen — leaving
    the exact specialization to the editor-mode step that follows.
    """
    deadline = time.monotonic() + WIZARD_VERIFY_TIMEOUT_MS / 1000
    state = ResumeState()
    while time.monotonic() < deadline:
        goto_hh(page, f"{HH_BASE_URL}/resume/{resume.resume_id}")
        require_authenticated_page(page)
        route_matches = resume_identity_matches(page, resume.resume_id) or is_position_wizard(
            page, resume.resume_id
        )
        if not route_matches:
            raise RuntimeError("post-save readback открыт не для того резюме")
        state = parse_resume_state(page.content(), resume.resume_id)
        if state.status is not None and state.next_incomplete_screen_id != "professional_role":
            return state
        page.wait_for_timeout(WIZARD_VERIFY_POLL_MS)

    if state.status is None:
        raise RuntimeError("post-save readback не подтвердил состояние резюме (wizard-minimum)")
    raise RuntimeError("post-save readback всё ещё показывает professional_role (wizard-minimum)")


def verify_wizard_save(
    page: Page,
    resume: ResumeConfig,
    *,
    expected_title: str,
    expected_role_id: str,
    expected_role_label: str,
) -> ResumeState:
    """Poll identity-bound state, role, and resume card until all confirm."""
    from .browser import RESUMES_FULL_LIST_URL
    from .copy_resume import list_resume_cards

    deadline = time.monotonic() + WIZARD_VERIFY_TIMEOUT_MS / 1000
    state = ResumeState()
    matches = []
    while time.monotonic() < deadline:
        goto_hh(page, f"{HH_BASE_URL}/resume/{resume.resume_id}")
        require_authenticated_page(page)
        route_matches = resume_identity_matches(page, resume.resume_id) or is_position_wizard(
            page, resume.resume_id
        )
        if not route_matches:
            raise RuntimeError("post-save readback открыт не для того резюме")
        state = parse_resume_state(page.content(), resume.resume_id)
        role_matches = any(
            role.role_id == expected_role_id
            and (role.label is None or role.label == expected_role_label)
            for role in state.professional_roles
        )
        if (
            state.status is not None
            and state.next_incomplete_screen_id != "professional_role"
            and role_matches
        ):
            cards = list_resume_cards(page, navigate=True, url=RESUMES_FULL_LIST_URL)
            matches = [card for card in cards if card.resume_id == resume.resume_id]
            if len(matches) == 1 and matches[0].title.strip() == expected_title.strip():
                return state
        page.wait_for_timeout(WIZARD_VERIFY_POLL_MS)

    if state.status is None:
        raise RuntimeError("post-save readback не подтвердил состояние резюме")
    if state.next_incomplete_screen_id == "professional_role":
        if logger.isEnabledFor(logging.DEBUG):
            _dump_wizard_failure(page, resume.resume_id, "post_save_professional_role")
        raise RuntimeError("post-save readback всё ещё показывает professional_role")
    observed_roles = ", ".join(
        f"{role.role_id}:{role.label or '?'}" for role in state.professional_roles
    )
    if not any(
        role.role_id == expected_role_id
        and (role.label is None or role.label == expected_role_label)
        for role in state.professional_roles
    ):
        raise WizardRoleMismatch(
            f"post-save professional role не совпал: ожидалось "
            f"{expected_role_id}:{expected_role_label}, прочитано "
            f"{observed_roles or '<пусто>'}"
        )
    if len(matches) != 1:
        raise RuntimeError(f"post-save readback карточки резюме неоднозначен: {len(matches)}")
    raise RuntimeError(
        f"post-save title не совпал: ожидалось «{expected_title}», прочитано «{matches[0].title}»"
    )


def _dump_control_failure(page: Page, selector: str, exc: Exception) -> None:
    """Best-effort DOM/screenshot dump on a _set_control failure (#561 review).

    A live single-value run failed without a captured trace, leaving the
    RESUME_POSITION_DROPDOWN geometry unverified (cycle-review UNVERIFIED).
    This dump turns the next live failure into recoverable evidence instead
    of silence.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", selector.strip("[]").replace("data-qa='", ""))
    try:
        (LOG_DIR / f"resume_position_{slug}_failure.html").write_text(
            page.content(), encoding="utf-8"
        )
        page.screenshot(path=str(LOG_DIR / f"resume_position_{slug}_failure.png"), full_page=True)
        logger.warning("resume_position: %s — дамп сохранён (%s)", selector, exc)
    except PlaywrightError as dump_exc:
        logger.warning("resume_position: %s — дамп недоступен: %s", selector, dump_exc)


def _set_control(page: Page, selector: str, value: str, labels: dict[str, str]) -> None:
    loc = page.locator(selector)
    if loc.count() != 1:
        raise RuntimeError(f"селектор формы не подтверждён: {selector}")
    el = loc.first
    tag = el.evaluate("e=>e.tagName")
    if tag in ("INPUT", "TEXTAREA"):
        el.fill(value)
        return
    panel = page.locator(RESUME_POSITION_DROPDOWN)
    try:
        # Magritte leaves the previous panel mounted after a selection.
        # Waiting for the panel itself avoids both an intercepted trigger
        # click and a stale option lookup on the next value. Explicit,
        # bounded timeouts (CLAUDE.md) turn a silent hang into a fast,
        # labeled failure instead of the default 30s per wait.
        panel.wait_for(state="hidden", timeout=_CONTROL_WAIT_TIMEOUT_MS)
        el.click()
        panel.wait_for(state="visible", timeout=_CONTROL_WAIT_TIMEOUT_MS)
        option = panel.get_by_role("option", name=labels[value], exact=True)
        if option.count() != 1:
            raise RuntimeError(f"вариант формы не найден: {labels[value]}")
        option.click()
        # Re-clicking the trigger (the earlier approach) does not close this
        # panel — confirmed live (#823): the panel re-opens/stays open even
        # after a genuine value change, not just a no-op selection. This
        # dropdown closes on an outside click instead, same as clicking away
        # from any Magritte popup; (0, 0) is always outside the panel, which
        # is positioned near the field, not the viewport corner. Escape is
        # deliberately avoided because it can close the whole editor form.
        page.mouse.click(0, 0)
        panel.wait_for(state="hidden", timeout=_CONTROL_WAIT_TIMEOUT_MS)
    except (PlaywrightError, RuntimeError) as exc:
        _dump_control_failure(page, selector, exc)
        raise


def _set_specializations(page: Page, values: list[str]) -> None:
    """Replace specializations through the confirmed nested tree selector."""
    if page.locator(SPECIALIZATION_ADD).count() != 1:
        raise RuntimeError("селектор добавления специализации не подтверждён")

    # The editor's delete buttons only change the pending form.  Removing all
    # existing cards first makes --specialization a replacement, not an append.
    cards = page.locator(SPECIALIZATION_DELETE)
    while cards.count():
        cards.first.click()

    page.locator(SPECIALIZATION_ADD).click()
    modal = page.locator(SPECIALIZATION_MODAL)
    if modal.count() != 1:
        raise RuntimeError("панель выбора специализаций не открылась")
    search = page.locator(SPECIALIZATION_SEARCH)
    submit = page.locator(SPECIALIZATION_SUBMIT)
    if search.count() != 1 or submit.count() != 1:
        raise RuntimeError("селектор панели специализаций не подтверждён")

    for value in values:
        search.fill(value)
        option = page.locator(SPECIALIZATION_OPTION).filter(
            has_text=re.compile(rf"^{re.escape(value)}$")
        )
        # search.fill() triggers an async React re-render of the filtered tree
        # (#822 live repro): reading option.count() right after fill() is a
        # race and can observe either the still-unfiltered default category
        # rows or a not-yet-rendered empty list, in both cases 0 matches for
        # a leaf that is genuinely present. Waiting for the first match (or a
        # bounded timeout confirming it never renders) turns that race into a
        # deterministic read, matching the wait_for(state="visible") pattern
        # this project already uses after every action that starts a React
        # render (CLAUDE.md, resume_position.py's own _set_control).
        try:
            option.first.wait_for(state="visible", timeout=_CONTROL_WAIT_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise RuntimeError(f"специализация не найдена в дереве резюме: {value}") from exc
        option_ids = {option.nth(index).get_attribute("data-qa") for index in range(option.count())}
        # The same leaf is rendered once under every matching parent category;
        # its data-qa id is the stable identity.  Different ids with the same
        # label are genuinely ambiguous because the CLI accepts labels only.
        if not option_ids or len(option_ids) != 1:
            raise RuntimeError(f"вариант специализации не найден однозначно: {value}")
        option.first.click()

    submit.click()
    # Waiting for the option itself is insufficient: hh.ru keeps the panel
    # mounted while applying the selection.
    modal.wait_for(state="hidden", timeout=10_000)


def apply_position(page: Page, plan: PositionValues, current: PositionValues | None = None) -> None:
    """Fill fields only. Caller owns confirmation and must click SAVE explicitly.

    ``current`` is the value already on the draft (from the just-opened form/
    display), used only to skip a no-op ``_set_control`` click (#823): a live
    run requesting the value already on the resume hung the full 5s timeout
    and failed a command that had nothing to change. Skipping keeps the
    request an honest no-op instead of clicking an option that was already
    selected. ``current=None`` (e.g. ``copy_resume``'s bare-title call)
    preserves the previous unconditional behaviour.
    """
    if plan.title == "":
        raise ValueError(
            "Пустой title отклоняется hh.ru. Укажите значение, например: "
            '--title "Python-разработчик". Если title не нужно менять, не передавайте --title.'
        )
    if plan.employment and len(plan.employment) > 1:
        raise RuntimeError(
            "несколько значений --employment не подтверждены на форме hh.ru: "
            "два независимых live-прогона (issue #526 review) не подтвердили, что "
            "сохраняются все переданные значения. Передайте одно значение."
        )
    if plan.work_format and len(plan.work_format) > 1:
        raise RuntimeError(
            "несколько значений --work-format не подтверждены на форме hh.ru: "
            "два независимых live-прогона (issue #526 review) не подтвердили, что "
            "сохраняются все переданные значения. Передайте одно значение."
        )
    if plan.specializations:
        _set_specializations(page, plan.specializations)
    if plan.title is not None:
        page.locator(TITLE).fill(plan.title)
    if plan.salary is not None:
        page.locator(SALARY).fill(str(plan.salary))
    if plan.currency is not None:
        # The data-qa element is a visible (not hidden) radio input inside a
        # Magritte chip <label>; its accessible name comes from the label's
        # Russian text (#785 live probe), not from the currency code, and the
        # role is "radio", not "button" — matching against role="button" or
        # against the currency code always misses. The radio is absolutely
        # positioned under the chip's <span> content, so clicking the radio
        # itself is intercepted by that span (confirmed live, #785); the
        # actual hit target is the wrapping <label> chip, which is the same
        # element get_by_role("radio", ...) resolves its accessible name
        # from, so its bounding box is used to click through the label.
        currency_input = page.locator(CURRENCY[plan.currency])
        if currency_input.count() != 1:
            raise RuntimeError(f"селектор валюты не подтверждён: {plan.currency}")
        currency_radio = page.get_by_role("radio", name=CURRENCY_LABELS[plan.currency], exact=True)
        if currency_radio.count() != 1:
            raise RuntimeError(f"переключатель валюты не подтверждён: {plan.currency}")
        currency_chip = currency_radio.locator("xpath=ancestor::label[1]")
        if currency_chip.count() != 1:
            raise RuntimeError(f"чип валюты не подтверждён: {plan.currency}")
        currency_chip.click()
    current_employment = current.employment if current else None
    if plan.employment:
        for value in plan.employment:
            if current_employment == [value]:
                continue
            _set_control(page, EMPLOYMENT, value, EMPLOYMENT_LABELS)
    current_work_format = current.work_format if current else None
    if plan.work_format:
        for value in plan.work_format:
            if current_work_format == [value]:
                continue
            _set_control(page, WORK_FORMAT, value, WORK_LABELS)
    if plan.commute and (current is None or current.commute != plan.commute):
        _set_control(page, TRAVEL, plan.commute, TRAVEL_LABELS)
    if plan.business_trips is not None and (
        current is None or current.business_trips != plan.business_trips
    ):
        _set_control(
            page,
            BUSINESS_TRIPS,
            "true" if plan.business_trips else "false",
            {"true": "Могу", "false": "Не могу"},
        )

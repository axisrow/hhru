"""LLM planning and UI editing for primary/additional resume education (#262).

The LLM produces a reviewable plan only. The browser writer never presses Save
in dry-run and uses only selectors confirmed by the authenticated read-only
research in issue #268.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import (
    HH_BASE_URL,
    PageStateIndeterminate,
    dismiss_cookie_banner,
    goto_hh,
    has_auth_cookie,
    has_login_form,
    labelled_field,
    open_hydrated_resume_editor,
    require_authenticated_page,
    require_available_resume,
    resume_identity_matches,
)
from .config_sections.education import EducationRecord
from .logging_setup import LOG_DIR
from .responses import NotAuthenticated

logger = logging.getLogger("hhru_bot.resume_education")

PRIMARY_TRIGGER = "[data-qa='resume-edit-button-education-{index}']"
ADDITIONAL_TRIGGER = "[data-qa='resume-edit-button-additionalEducation-{index}']"
# Confirmed by a read-only live DOM probe on the dedicated training resume
# 11112222333344445555666677778888999900 (2026-08-18). These links only open
# the form; they do not persist anything until SAVE_BUTTON is clicked.
PRIMARY_ADD = "[data-qa='resume-list-card-education'] [data-qa='link']"
ADDITIONAL_ADD = "[data-qa='resume-list-card-additionalEducation'] [data-qa='link']"
# #814: у уже существующей записи (index=0 присутствует) hh.ru открывает
# /profile/edit/primaryEducation/{entry_id}?resumeFrom=... -- с id-хвостом,
# а не /profile/edit/primaryEducation без хвоста (тот путь — только для
# пустой секции через PRIMARY_ADD, см. живой DOM issue #814). Опциональный
# `(?:/[^/?#]+)?` покрывает обе ветки. Guard от чужого резюме держится не на
# этом regex (он проверяет только экран), а на отдельной проверке
# `expected_query={"resumeFrom": resume_id}` в вызове ниже -- расширение
# хвоста id записи её не ослабляет.
PRIMARY_ROUTE = re.compile(r"/profile/edit/primaryEducation(?:/[^/?#]+)?(?=[?#]|$)")
ADDITIONAL_ROUTE = re.compile(r"/profile/edit/additionalEducation/[^/?#]+")
# The two education editors are DIFFERENT screens with different controls, so a
# single pair of buttons cannot serve both (live probe 2026-08-30):
#   primary    -> /profile/edit/primaryEducation      -> profile-layout-*
#   additional -> /resume/edit/<id>/additionalEducation -> resume-partial-edit-*
# Each candidate is count=0 on the other screen; keep them separate rather than
# guessing one shared id.
# #857 (live probe 2026-08-30): the additionalEducation CARD on the resume page
# renders ONLY when at least one additional entry is already attached to the
# resume (entries are profile-level and are attached on the wizard's educations
# screen). A resume with zero attached additional entries has neither the card
# nor the Add link -- but the resume-scoped direct route below still renders
# the full resume-partial-edit form (confirmed live on such a resume). It is
# the only confirmed way in for that case, so _edit_block falls back to it.
ADDITIONAL_DIRECT_PATH = "/resume/edit/{resume_id}/additionalEducation"
# The primary education card is the always-present hydration marker used for
# the additional block's pre-loop wait (see _edit_block): #812's invariant
# ("Уровень образования" field always exists) means the card renders on every
# resume page even when the additional card legitimately does not.
PRIMARY_EDUCATION_CARD = "[data-qa='resume-list-card-education']"
CANCEL_BUTTON = "[data-qa='profile-layout-cancel-button']"
SAVE_BUTTON = "[data-qa='profile-layout-save-button']"
ADDITIONAL_CANCEL_BUTTON = "[data-qa='resume-partial-edit-cancel']"
ADDITIONAL_SAVE_BUTTON = "[data-qa='resume-partial-edit-save']"

# #802: deletion by hh.ru entry id (the numeric hash from
# /profile/edit/{kind}Education/{id}, e.g. from URL after a manual entry).
# The id-scoped edit route is distinct from PRIMARY_ROUTE/ADDITIONAL_ROUTE
# above (those bind by resumeFrom+index for add/prefill, not by entry id) and
# was confirmed by a live authenticated read-only probe of a real profile
# record (2026-08-18/29, data/logs/resume-edit*-resume-edit-button-*Education-*.html):
# the canonical link on both the primary and additional education forms is
# https://hh.ru/profile/edit/{kind}Education/{id}. The delete button's data-qa
# is confirmed the same way, in the same dumps -- oddly reusing the
# "experience" name on the PRIMARY education form (hh.ru shares one row-editor
# React component between education and experience blocks); this is the real
# observed value, not a guess.
_ENTRY_ROUTE = {
    "primary": "primaryEducation",
    "additional": "additionalEducation",
}
_ENTRY_DELETE_TRIGGER = {
    "primary": "[data-qa='resume-partial-edit-experience-delete']",
    "additional": "[data-qa='resume-partial-edit-additional-education-delete']",
}
ENTRY_DELETE_VERIFY_TIMEOUT_MS = 15_000

_PRIMARY_FIELDS = {
    "institution": "[data-qa='profile-education-university-input']",
    "faculty": "[data-qa='profile-education-faculty-input']",
    "specialty": "[data-qa='profile-education-specialty-input']",
    "year": "[data-qa='profile-education-year-input']",
}
# The additional-education form opened through the DIRECT route carries NO
# data-qa on any of its inputs — they are bound only through aria-labelledby +
# <label> (Magritte, live probe 2026-08-30; every profile-education-additional-*
# candidate is count=0 there). The visible label is the only stable handle,
# addressed through browser.labelled_field, which requires one exact match and
# fails closed.
_ADDITIONAL_LABELS = {
    "institution": "Название",
    "organization": "Проводившая организация",
    "specialty": "Специализация",
    "year": "Год окончания",
}
# #857 (live drill, 2026-08-30): the SAME semantic form opened through the
# resume card's row trigger is a DIFFERENT shape -- its inputs carry data-qa
# and their <label> elements bind with EMPTY text (dumped live on the trigger-
# opened form), so get_by_label is unreliable there: it resolved and verified
# an institution fill that hh.ru then did not persist (2 of 3 trigger-shape
# saves took the value, one silently reverted -- same fill-then-reset race
# class as #825, but the address itself is the weak link). These data-qa were
# confirmed by dumping the opened form; year reuses the primary editor's year
# input.
_ADDITIONAL_TRIGGER_SHAPE_FIELDS = {
    "institution": "[data-qa='profile-education-additional-name']",
    "organization": "[data-qa='profile-education-additional-organization']",
    "specialty": "[data-qa='profile-education-additional-specialty']",
    "year": "[data-qa='profile-education-year-input']",
}
FORM_TIMEOUT_MS = 15_000
# #825: было отсутствие явного timeout у wait_for_url после клика Save, из-за
# чего неудача проявлялась только после дефолтных 90с Playwright
# (browser.GOTO_TIMEOUT_MS, установленных context-wide) -- задержка сама по
# себе ничего не доказывала и маскировала настоящую причину (см. комментарий
# у save.click() ниже). 20с — тот же порядок, что SAVE_TIMEOUT_MS в
# experience.py (#811): достаточно для медленного hh.ru после реального
# сохранения (навигация происходит за секунды по всем живым прогонам), но не
# держит CLI минуты на заведомо неуспешном пути.
SAVE_TIMEOUT_MS = 20_000
# The URL can commit before the resume page's React card is hydrated.  Wait for
# the saved row itself before treating identity/text checks as authoritative;
# this timeout is local to the post-save resume screen, not a browser-wide
# default.
POST_SAVE_RESUME_WAIT_TIMEOUT_MS = 15_000
# #825: `open_hydrated_resume_editor`'s hydration marker is the primary form's
# own institution INPUT becoming visible (see editor_selector below) -- but
# "visible" only proves the empty <input> node exists in the DOM, not that
# hh.ru's Magritte controlled component has finished initializing its React
# state. A live dump caught the exact race: institution/year were filled,
# Save was clicked, and hh.ru's own client-side validation rejected the
# submit because the fields were empty on the DOM at click time -- the
# `.fill()` calls landed, but a subsequent React re-render (still settling
# right after the editor "became visible") reset the controlled inputs back
# to their initial empty value, wiping out the just-typed text before Save
# ever read it. This is the same "commit не значит отрисовано" class already
# fixed for resume_position.py's wizard title field (WIZARD_VERIFY_POLL_MS) --
# poll input_value() after fill() and retry rather than trust a single
# best-effort `.fill()`.
FIELD_VERIFY_TIMEOUT_MS = 3_000
FIELD_VERIFY_POLL_MS = 200
# #956: budget for the all-fields re-check right before the Save click.
PRE_SAVE_STABLE_TIMEOUT_MS = 5_000
PRE_SAVE_STABLE_POLL_MS = 250


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


def _field_locator(page, name: str, *, additional: bool, trigger_shape: bool = False):
    """Resolve one education field on whichever of the additional shapes is open.

    All paths require exactly one match and raise otherwise, so a drifted
    selector or an ambiguous label stops the write instead of filling the
    wrong control. #857: the additional block has TWO shapes -- direct-route
    (label-addressed) and trigger-opened (data-qa, see
    _ADDITIONAL_TRIGGER_SHAPE_FIELDS); primary stays data-qa.
    """
    if additional:
        if trigger_shape:
            selector = _ADDITIONAL_TRIGGER_SHAPE_FIELDS[name]
            locator = page.locator(selector)
            if locator.count() != 1:
                raise PageStateIndeterminate(f"поле {selector} не найдено однозначно")
            return locator
        return labelled_field(page, _ADDITIONAL_LABELS[name])
    locator = page.locator(_PRIMARY_FIELDS[name])
    if locator.count() != 1:
        raise PageStateIndeterminate(f"поле {_PRIMARY_FIELDS[name]} не найдено однозначно")
    return locator


def _fill_and_verify(page, locator, value: str) -> bool:
    """Fill a field and confirm the value actually stuck (#825).

    ``.fill()`` succeeding is not proof the value survives -- a Magritte
    controlled input can reset itself to its initial (often empty) state in
    an async React re-render that lands moments after the editor's hydration
    marker became visible (see FIELD_VERIFY_TIMEOUT_MS comment above). Retry
    the fill within a short budget instead of trusting a single attempt;
    return False (never raise) so the caller can fail the row closed with a
    clear reason rather than clicking Save on a field the DOM disagrees with.
    Uses page.wait_for_timeout (not time.sleep) to match the polling idiom
    already used for this same class of race elsewhere in the project
    (resume_position.py's WIZARD_VERIFY_POLL_MS, skills.py's CHIP_COMMIT_POLL_MS).
    """
    # Review (PR #855): compare trimmed, not exact -- hh.ru may normalize the
    # DOM value (collapsing/trimming whitespace) without that meaning the
    # fill was lost. An exact match would retry the full budget and then
    # falsely fail a field hh.ru genuinely accepted, just reformatted. This
    # still catches the real #825 defect (value reverts to "").
    expected = value.strip()
    deadline = time.monotonic() + FIELD_VERIFY_TIMEOUT_MS / 1000
    while True:
        locator.fill(value)
        if locator.input_value().strip() == expected:
            # #956: an async React re-render can remount the controlled input
            # AFTER the immediate check -- the DOM value fill() wrote is then
            # replaced by the (often empty) React state, and Save submits an
            # empty form (live failure 2026-09-03: save-failure dump showed
            # "Пожалуйста, укажите" on fields _fill_and_verify had confirmed).
            # Wait one poll interval past the match and re-check before
            # trusting the value.
            page.wait_for_timeout(FIELD_VERIFY_POLL_MS)
            if locator.input_value().strip() == expected:
                return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(FIELD_VERIFY_POLL_MS)


def _pre_save_stable(
    page, filled: list[tuple[str, str]], *, additional: bool, trigger_shape: bool
) -> bool:
    """Re-check every filled field right before the Save click (#956).

    Each field is verified at fill time, but the clearing re-render may land
    after the LAST field was verified -- the live save-failure dump showed ALL
    fields empty with client-side validation marks at the click. Refill
    whatever the form reset and require one full clean pass before saving.
    """
    deadline = time.monotonic() + PRE_SAVE_STABLE_TIMEOUT_MS / 1000
    resolved: list[tuple[Any, str]] = []
    for name, value in filled:
        resolved.append(
            (
                _field_locator(page, name, additional=additional, trigger_shape=trigger_shape),
                value,
            )
        )
    while True:
        cleared = [
            (locator, value)
            for locator, value in resolved
            if locator.input_value().strip() != value.strip()
        ]
        if cleared:
            for locator, value in cleared:
                locator.fill(value)
        # A matching value can still be replaced by the controlled-input
        # remount immediately after this check. Require the complete set to
        # survive one poll before allowing Save.
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(PRE_SAVE_STABLE_POLL_MS)
        if all(locator.input_value().strip() == value.strip() for locator, value in resolved):
            return True


def _dump_save_failure(page, index: int, kind: str, exc: Exception) -> None:
    """Best-effort DOM/screenshot dump on a fill or post-save-click failure (#825).

    Two distinct failure points share this helper: a field whose value did
    not survive ``fill()`` (``_fill_and_verify``, BEFORE Save is ever
    clicked) and a save-click outcome that could not be confirmed
    (`save.click()` followed by a timed-out `wait_for_url`, or an unconfirmed
    identity/text check, AFTER the click). Both previously left no trace to
    diagnose beyond reproducing them live by hand. Mirrors
    ``resume_position._dump_control_failure`` -- same pattern, applied
    to this module's own failure point.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"resume_education_{kind}_{index}_save_failure"
    try:
        (LOG_DIR / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(LOG_DIR / f"{stem}.png"), full_page=True)
        logger.warning("resume_education: %s строка %d — дамп сохранён (%s)", kind, index, exc)
    except Exception as dump_exc:  # noqa: BLE001 - диагностика best-effort не должна ронять команду
        # #825 review: page.content()/page.screenshot() могут бросить не только
        # PlaywrightError (например TargetClosedError на уже закрытом контексте
        # в отдельных версиях Playwright) -- этот хелпер существует только ради
        # диагностики, поэтому любая его собственная ошибка должна логироваться
        # и глушиться, а не всплывать наружу вместо настоящего результата команды.
        logger.warning(
            "resume_education: %s строка %d — дамп недоступен: %s", kind, index, dump_exc
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
    route = ADDITIONAL_ROUTE if additional else PRIMARY_ROUTE
    save_selector = ADDITIONAL_SAVE_BUTTON if additional else SAVE_BUTTON
    cancel_selector = ADDITIONAL_CANCEL_BUTTON if additional else CANCEL_BUTTON
    # Field names are identical for both blocks; only the way a field is
    # addressed differs (data-qa on primary, visible label on additional).
    field_names = tuple(_ADDITIONAL_LABELS if additional else _PRIMARY_FIELDS)
    kind = "additional" if additional else "primary"
    saved_count = 0
    if not records:
        return EducationResult(kind, True, "нет записей для изменения")
    # #812: goto_hh only guarantees URL commit, not rendered DOM (CLAUDE.md,
    # "commit не значит отрисовано") -- the resume page hydrates the
    # education card asynchronously, and a strict count() right after goto_hh
    # can race it and see 0 even though the card renders moments later. The
    # "Уровень образования" field always exists on hh.ru (unlike about/skills,
    # this section is never legitimately absent), so exactly one of the
    # possible markers must eventually appear. #857: for the additional block
    # the card and Add link may BOTH legitimately never render (no attached
    # entries), so the always-present primary education card completes the
    # marker set there; the per-row logic below reaches the form through the
    # direct route in that case.
    pre_loop = page.locator(trigger.format(index=0)).or_(page.locator(add_selector))
    if additional:
        pre_loop = pre_loop.or_(page.locator(PRIMARY_EDUCATION_CARD))
    try:
        pre_loop.first.wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        return EducationResult(
            kind,
            False,
            f"блок образования не отобразился за {FORM_TIMEOUT_MS}мс: {exc}",
        )
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
            if button.count() != 1 and not additional:
                return EducationResult(
                    kind,
                    False,
                    f"строка образования {index} отсутствует, подтвержденная кнопка Добавить "
                    "не найдена однозначно",
                    uncertain=saved_count > 0,
                    saved=saved_count,
                )
        # #857: additional with neither an existing row trigger nor the Add
        # link (no card at all -- zero attached profile entries) opens the
        # form through its resume-scoped direct route instead. The resume_id
        # is part of the URL, so the identity check below binds the form to
        # the right resume; nothing is scoped to a clickable trigger that
        # does not exist.
        direct_route = additional and button_count == 0 and button.count() != 1
        # #857 (live drill): the additional form renders TWO control shapes
        # depending on how it was opened. Opened through the direct route it
        # carries resume-partial-edit-save/cancel (as the 2026-08-30 probe
        # that picked these selectors saw); opened through the resume card's
        # row trigger it renders profile-layout-save-button/cancel-button --
        # the SAME controls as the primary editor -- and the
        # resume-partial-edit buttons are count=0 there (confirmed live by
        # clicking the trigger and dumping the controls, no save pressed).
        # The editor hydration marker follows the same choice.
        if additional and not direct_route:
            row_save_selector = SAVE_BUTTON
            row_cancel_selector = CANCEL_BUTTON
        else:
            row_save_selector = save_selector
            row_cancel_selector = cancel_selector
        save_clicked = False
        try:
            if direct_route:
                goto_hh(page, f"{HH_BASE_URL}{ADDITIONAL_DIRECT_PATH.format(resume_id=resume_id)}")
                editor_marker = page.locator(row_save_selector)
                editor_marker.first.wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
                current_path = urlsplit(page.url).path
                path_parts = [part for part in current_path.split("/") if part]
                if path_parts != ["resume", "edit", resume_id, "additionalEducation"]:
                    raise RuntimeError(
                        f"форма доп. образования открыта не для того резюме: {page.url}"
                    )
            else:
                open_hydrated_resume_editor(
                    page,
                    trigger_selector=(
                        trigger.format(index=index) if button_count == 1 else add_selector
                    ),
                    # The additional form exposes no data-qa on its inputs, so its
                    # hydration marker is the editor's own save control (live probe
                    # 2026-08-30: present on the rendered form, absent before it).
                    editor_selector=(
                        row_save_selector if additional else _PRIMARY_FIELDS["institution"]
                    ),
                    profile_path=f"/resume/{resume_id}",
                    edit_path=route,
                    timeout=FORM_TIMEOUT_MS,
                    trigger_error=f"триггер образования {index} не найден однозначно",
                    open_error=f"форма образования {index} не открылась",
                    wrong_route_error=f"форма образования {index} открыта не для того резюме",
                    expected_query={"resumeFrom": resume_id} if not additional else None,
                )
            for name in field_names:
                value = getattr(record, name)
                # Empty LLM fields mean "unknown", not "erase the current value".
                # This protects prefill and also makes a partial from-scratch plan
                # fail closed rather than destroy data already on hh.ru.
                if not value:
                    continue
                try:
                    locator = _field_locator(
                        page,
                        name,
                        additional=additional,
                        trigger_shape=additional and not direct_route,
                    )
                except PageStateIndeterminate as exc:
                    return EducationResult(
                        kind,
                        False,
                        str(exc),
                        uncertain=saved_count > 0,
                        saved=saved_count,
                    )
                if not _fill_and_verify(page, locator, value):
                    # #825: the field accepted fill() but the DOM value did not
                    # stick within FIELD_VERIFY_TIMEOUT_MS -- clicking Save on a
                    # field the form itself disagrees with only reproduces the
                    # observed live failure (hh.ru's client-side validation
                    # rejects the empty field, Save no-ops, wait_for_url times
                    # out). No click has happened yet on this row, so this is a
                    # clean pre-click failure, not uncertain.
                    _dump_save_failure(
                        page,
                        index,
                        kind,
                        RuntimeError(f"поле {name!r} не сохранило значение после fill()"),
                    )
                    return EducationResult(
                        kind,
                        False,
                        f"строка {index}: поле {name!r} не приняло значение "
                        f"(осталось {locator.input_value()!r} вместо {value!r})",
                        uncertain=saved_count > 0,
                        saved=saved_count,
                    )
            # #956: per-field verification happened during the loop above, but
            # the clearing re-render may land after the LAST field was verified
            # (live dump: every field empty at the click). Fail closed BEFORE
            # clicking Save if the form would not hold the values.
            filled_fields = [
                (name, getattr(record, name)) for name in field_names if getattr(record, name)
            ]
            if not _pre_save_stable(
                page,
                filled_fields,
                additional=additional,
                trigger_shape=additional and not direct_route,
            ):
                _dump_save_failure(
                    page,
                    index,
                    kind,
                    RuntimeError("форма сбросила значения полей перед сохранением"),
                )
                return EducationResult(
                    kind,
                    False,
                    f"строка {index}: поля сброшены формой перед сохранением, Save не нажат",
                    uncertain=saved_count > 0,
                    saved=saved_count,
                )
            if dry_run:
                page.locator(row_cancel_selector).first.click()
            else:
                save = page.locator(row_save_selector)
                if save.count() != 1:
                    return EducationResult(
                        kind,
                        False,
                        "кнопка сохранения не найдена однозначно",
                        uncertain=saved_count > 0,
                        saved=saved_count,
                    )
                # #825: живой прогон подтвердил, что hh.ru показывает информер
                # cookie-политики fixed внизу экрана на свежей навигации, и он
                # может оставаться в DOM 40+ секунд -- всё это время он
                # физически перекрывает кнопку Save, и клик по перекрытому
                # узлу молча не долетает до формы (см. комментарий у
                # dismiss_cookie_banner в browser.py). Дисмисс — best-effort
                # прямо перед кликом, а не один раз при открытии страницы: то
                # же самое расследование увидело баннер как до, так и после
                # открытия формы редактирования.
                dismiss_cookie_banner(page)
                save_clicked = True
                save.click()
                navigation_error: PlaywrightError | None = None
                try:
                    # Trailing "**" (#958 follow-up, #960): the post-save
                    # redirect carries a query suffix (live log 2026-09-03,
                    # experience editor: navigated to ".../resume/{id}?hhtmFrom=
                    # profile_experience") — and a bare glob is a FULL match, so
                    # the wait would time out although the navigation happened.
                    # "**" (not "*") also matches a trailing slash, i.e. the
                    # "/resume/{id}/" redirect shape from PR #958 review
                    # cycle 3; the identity checks below still guard the result.
                    page.wait_for_url(
                        f"**/resume/{resume_id}**", wait_until="commit", timeout=SAVE_TIMEOUT_MS
                    )
                except PlaywrightError as exc:
                    # #825: раньше здесь не было явного timeout -- Playwright
                    # дефолтился на context-wide GOTO_TIMEOUT_MS (90с), так что
                    # неудача проявлялась только через полторы минуты и без
                    # единой зацепки, почему save не сработал. Короткий явный
                    # SAVE_TIMEOUT_MS делает отказ быстрым, а дамп страницы
                    # прямо в момент отказа делает следующее расследование
                    # воспроизводимым без повторного похода в live-браузер.
                    #
                    # Живой прогон нашёл конкретный случай (аналог #179 из
                    # CLAUDE.md для формы отклика): hh.ru сменил page.url на
                    # адрес резюме и запись реально появилась в DOM, но
                    # wait_for_url(wait_until="commit") всё равно истёк по
                    # таймауту -- SPA/pushState-навигация не обязана поднимать
                    # то же lifecycle-событие документа, которого ждёт
                    # Playwright. Таймаут здесь -- НЕ доказательство неудачи
                    # (то же рассуждение, что и для "коммит не значит
                    # отрисовано" в CLAUDE.md, только в обратную сторону):
                    # если identity и текст записи всё же подтверждаются,
                    # результат success, а не ложный uncertain.
                    navigation_error = exc
                # ``commit`` only proves navigation, not React hydration.  The
                # saved row is a screen-local marker that cannot be present on
                # the editor route; waiting for it separates a slow render from
                # an actually missing/incorrect resume route.  In particular,
                # this also gives the URL a chance to settle after a SPA
                # pushState navigation that timed out in Playwright.
                post_save_marker_selector = (
                    PRIMARY_EDUCATION_CARD if direct_route else trigger.format(index=index)
                )
                resume_marker = page.locator(post_save_marker_selector).first
                try:
                    resume_marker.wait_for(
                        state="visible", timeout=POST_SAVE_RESUME_WAIT_TIMEOUT_MS
                    )
                except PlaywrightError as marker_exc:
                    logger.warning(
                        "resume_education: post-save marker unavailable; url=%s marker_count=%s "
                        "navigation_error=%s marker_error=%s",
                        page.url,
                        page.locator(post_save_marker_selector).count(),
                        navigation_error,
                        marker_exc,
                    )
                    failure = navigation_error or marker_exc
                    _dump_save_failure(page, index, kind, failure)
                    return EducationResult(
                        kind,
                        False,
                        f"сохранение не подтверждено после клика: {failure}",
                        uncertain=True,
                        saved=saved_count,
                    )
                logger.info(
                    "resume_education: post-save marker visible; url=%s marker_count=%s "
                    "navigation_error=%s",
                    page.url,
                    page.locator(post_save_marker_selector).count(),
                    navigation_error,
                )
                if not resume_identity_matches(page, resume_id):
                    # #825 review: dump the exact post-hydration state rather
                    # than attributing a selector race to identity blindly.
                    _dump_save_failure(
                        page, index, kind, RuntimeError("identity резюме не подтверждён")
                    )
                    return EducationResult(
                        kind,
                        False,
                        "после сохранения identity резюме не подтверждён",
                        uncertain=True,
                        saved=saved_count,
                    )
                # #825: navigation back to the resume page is not itself proof
                # the record was written -- live investigation found a case
                # where a field silently reverted to empty (Magritte combobox
                # race, unrelated to this click) with the URL still changing
                # normally. The positive signal is the record's own institution
                # text now visible on the resume page, mirroring the
                # reload-and-recount check already used for experience rows
                # (#787/experience.py) -- proportional here to one text() read
                # rather than a full recount, since education entries are
                # addressed by index, not by a growing count of new rows.
                #
                # #825 review: an empty institution_value (both institution and
                # organization blank) must not silently skip this check --
                # _record()/CLI manual-entry parsing already require a non-empty
                # institution before a plan reaches this function, so an empty
                # value here means the record itself is malformed in a way that
                # earlier validation should have caught. Fail closed rather than
                # treat "nothing to check" as "verified".
                institution_value = record.institution or record.organization
                if not institution_value:
                    return EducationResult(
                        kind,
                        False,
                        f"строка {index}: запись без institution/organization -- "
                        "результат сохранения не проверяем",
                        uncertain=True,
                        saved=saved_count,
                    )
                # #857 (live drill): the text check races the SPA's hydration
                # of the resume page -- wait_for_url(commit) confirms the URL,
                # not the rendered card, so an immediate get_by_text().count()
                # saw 0 for a record hh.ru had genuinely saved (the record
                # appeared once hydration finished, confirmed by a follow-up
                # read-only probe). Poll for the text within a bounded budget
                # (same "commit не значит отрисовано" class as the pre-loop
                # wait above; FORM_TIMEOUT_MS, not FIELD_VERIFY_TIMEOUT_MS --
                # the live drill's card rendered well after 3s) instead of
                # trusting a single immediate read.
                text_deadline = time.monotonic() + FORM_TIMEOUT_MS / 1000
                while True:
                    if page.get_by_text(institution_value).count() > 0:
                        break
                    if time.monotonic() >= text_deadline:
                        _dump_save_failure(
                            page,
                            index,
                            kind,
                            RuntimeError(
                                f"{institution_value!r} не найден на резюме после сохранения"
                            ),
                        )
                        return EducationResult(
                            kind,
                            False,
                            f"строка {index}: запись не отображается на резюме после сохранения "
                            f"({institution_value!r} не найден)",
                            uncertain=True,
                            saved=saved_count,
                        )
                    page.wait_for_timeout(FIELD_VERIFY_POLL_MS)
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
    # #972: сбойный экран /resume/{id} держит URL — внятный отказ вместо
    # таймаута на поиске секций образования. Pre-mutation, failed/retry.
    require_available_resume(page)
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


@dataclass(frozen=True)
class EducationDeleteResult:
    """Result of deleting one education entry by its hh.ru entry id (#802).

    ``not_found`` (True) means the id-scoped edit route did not render the
    entry's delete control and hh.ru's visible error boundary
    (``_ENTRY_NOT_FOUND_TEXT``) confirms it is not a mere hydration/selector
    miss -- the entry does not exist. This is checked both before the click
    (a stale/wrong id is a plain, resolvable failure) and, structurally, is
    also what a resolved retry after an ``uncertain`` outcome hits on its
    next run (the entry the earlier click removed is now equally "not
    found"). That is what makes retrying a durable
    ``delete_education_entry`` attempt after an ``uncertain`` outcome
    structurally resolvable to ``success`` -- unlike delete-resume's #480
    trap, a repeat run does not need the click to fire again: the entry is
    either still there (retry the click) or already gone (report success).
    Note hh.ru's page.url does NOT change for a missing entry (live-confirmed
    2026-08-30) -- do not reintroduce a route-based check here.
    """

    entry_id: str
    kind: str
    success: bool
    reason: str
    uncertain: bool = False
    not_found: bool = False


# #802 live read-only probe (2026-08-30, https://hh.ru/profile/edit/primaryEducation/1
# against a real authenticated session): a nonexistent entry id does NOT
# redirect the URL away from the id-scoped route (confirmed: page.url stays
# exactly on /profile/edit/primaryEducation/1). hh.ru instead renders a
# React error boundary with this exact visible text inside <div
# class="row-content">, replacing the whole form -- no data-qa attribute is
# attached to it, so the visible text is the only confirmed signal. This
# supersedes an earlier draft of this module that assumed a route mismatch
# was the not-found signal; that assumption was live-disproven before merge.
_ENTRY_NOT_FOUND_TEXT = "Problem fetching content"

# #809 live read-only probe (2026-08-30, https://hh.ru/profile/edit/
# primaryEducation/104505393, real authenticated session, cancelled via
# "Отменить" -- no mutation performed): clicking the form's "Удалить" button
# (_ENTRY_DELETE_TRIGGER above) does NOT delete the entry directly. It opens a
# SECOND Magritte confirm dialog ("Учёба исчезнет из всех резюме, где она
# есть. Всё равно удалить?") rendered as `[role="alertdialog"][aria-modal=
# "true"]`. Neither the dialog nor its two buttons carry any `data-qa`
# attribute (confirmed via page JS: both `outerHTML` dumps have `dataQa:
# null`); their CSS classes are hashed build artifacts
# (`magritte-button___Pubhr_7-2-26` etc.) and are NOT a stable selector across
# hh.ru deploys. The only stable handle is the dialog's ARIA role plus its
# buttons' exact visible text -- the same text-based, fail-closed pattern
# already used by `_ENTRY_NOT_FOUND_TEXT` above and by
# `labelled_field`/`get_by_text` elsewhere in this project. This was
# confirmed only for kind="primary"; the #809 report notes "additional"
# education/experience deletions were NOT observed to show this second
# dialog, so the wait below treats it as OPTIONAL (either outcome -- dialog
# shown, or the row torn down directly -- is accepted) rather than required.
_DELETE_CONFIRM_DIALOG = "[role='alertdialog'][aria-modal='true']"
_DELETE_CONFIRM_BUTTON_TEXT = "Удалить"
# #809: bounds each attempt of the click-retry loop below that works around
# this route's hydration lag (see the loop's own comment). 4 attempts x 5s =
# 20s worst case before falling back to uncertain -- live-confirmed hydration
# completed within ~10s in the investigation that found this race, so this
# leaves headroom without picking an arbitrarily large single wait.
DELETE_CONFIRM_DIALOG_TIMEOUT_MS = 5_000
DELETE_CLICK_MAX_ATTEMPTS = 4


def delete_education_entry_on_hh(
    page,
    kind: str,
    entry_id: str,
    dry_run: bool,
    *,
    before_click=None,
) -> EducationDeleteResult:
    """Delete exactly one education entry, addressed by its hh.ru id (#802).

    ``kind`` is ``"primary"`` or ``"additional"`` -- the two forms live on
    distinct routes with distinct delete-button data-qa (see
    ``_ENTRY_ROUTE``/``_ENTRY_DELETE_TRIGGER`` above). The entry is not
    scoped to a resume_id: hh.ru's profile-level education records can be
    orphaned (belong to no resume, or to a resume already deleted), matching
    the exact scenario in #802's motivating cleanup case.
    """
    if kind not in _ENTRY_ROUTE:
        raise ValueError(f"kind должен быть 'primary' или 'additional', получено: {kind!r}")

    goto_hh(page, f"{HH_BASE_URL}/profile/edit/{_ENTRY_ROUTE[kind]}/{entry_id}")
    require_authenticated_page(page)
    button = page.locator(_ENTRY_DELETE_TRIGGER[kind])
    if button.count() != 1:
        if page.get_by_text(_ENTRY_NOT_FOUND_TEXT).count() > 0:
            # #802 vs #480: the id-scoped edit route rendered hh.ru's error
            # boundary instead of the form -- confirmed live (2026-08-30) to
            # NOT change page.url, so a route check alone cannot see this;
            # the visible error text is the only signal. Both a first attempt
            # on a bad/stale id and a retry after a resolved deletion look
            # identical here -- structurally indistinguishable without a
            # second, independent read of the entry, which does not exist
            # for a profile-level record. Report success: the caller only
            # ever invokes this to make the entry not exist, and it does not.
            return EducationDeleteResult(
                entry_id,
                kind,
                True,
                f"запись {entry_id} не найдена; уже отсутствует",
                not_found=True,
            )
        return EducationDeleteResult(
            entry_id, kind, False, "кнопка удаления записи не подтверждена однозначно"
        )

    if dry_run:
        return EducationDeleteResult(entry_id, kind, True, "dry-run; кнопка удаления не нажата")

    # #809 live investigation (2026-08-30, real account, headless AND headed):
    # the id-scoped edit route's SSR markup renders the delete button visible
    # well before React finishes hydrating THIS node -- confirmed via
    # `__reactFiber*`/`__reactProps*` presence-walk up the DOM tree, which
    # showed 0 of 8 ancestor levels hydrated shortly after navigation and all
    # 8 hydrated ~10s later. This is the same "visible != гидратирован" class
    # of race already documented in CLAUDE.md for other hh.ru SSR screens, but
    # here it hits an unusual extreme (single-digit-second hydration lag on
    # this specific route/bundle, `profile_primaryEducation-route.*.js`).
    # Playwright's `.click()` succeeds either way (the plain DOM node exists
    # and is actionable) but is a silent no-op before hydration: neither the
    # confirm dialog nor any network request follows. The first click is
    # confirmed non-destructive for kind="primary" (it only opens the second
    # dialog); for kind="additional" it is UNCONFIRMED whether the click
    # itself is what tears the row down (see the no-dialog branch below). The
    # reservation below is therefore made ONCE, before this loop's first
    # click, rather than after any click or per retry iteration: for
    # "primary" that is provably safe (the click is not destructive), and for
    # "additional" it is strictly safer than reserving after the click --
    # reserving early can only cost a spurious ``uncertain`` row on a click
    # that never lands (the project's existing fail-closed trade-off, #476),
    # while reserving after a possibly-destructive click leaves a real crash
    # window with NO recorded row at all (AO review on PR #816, action_id
    # stays None if the process dies before this call, and interrupt() is a
    # no-op for that state -- silently diverging the audit trail from
    # hh.ru's real state, worse than delete-resume's #480 lockout).
    if before_click is not None:
        before_click()
    click_attempts = 0
    dialog = page.locator(_DELETE_CONFIRM_DIALOG)
    trigger = page.locator(_ENTRY_DELETE_TRIGGER[kind])
    dialog_appeared = False
    last_exc: PlaywrightError | None = None
    while click_attempts < DELETE_CLICK_MAX_ATTEMPTS:
        click_attempts += 1
        try:
            button.first.click()
        except PlaywrightError as exc:
            return EducationDeleteResult(
                entry_id, kind, False, f"ошибка UI-клика: {exc}", uncertain=True
            )
        # Wait for either observed outcome -- the second confirm dialog
        # renders (kind="primary", live-confirmed), or the row is torn down
        # directly without one (the #809 report's unconfirmed claim for
        # "additional"/experience deletions). ``trigger`` is already visible
        # before this click (checked via ``button.count() == 1`` above), so
        # racing a "visible" wait on it here would resolve immediately and
        # never give the dialog time to render -- the row-torn-down outcome
        # must be observed as ``trigger`` DETACHING instead, mirroring the
        # wait used later in this function for the no-dialog case.
        try:
            dialog.first.wait_for(state="visible", timeout=DELETE_CONFIRM_DIALOG_TIMEOUT_MS)
            dialog_appeared = True
            last_exc = None
            break
        except PlaywrightError:
            try:
                trigger.first.wait_for(state="detached", timeout=DELETE_CONFIRM_DIALOG_TIMEOUT_MS)
                last_exc = None
                break
            except PlaywrightError as exc:
                # Neither outcome rendered -- most likely this attempt's
                # click landed before hydration reached this node (see
                # comment above). Retry the click rather than failing
                # immediately; only exhausting all attempts is uncertain.
                last_exc = exc
                continue
    if last_exc is not None:
        # The click(s) already reached hh.ru (button.first.click() above did
        # not raise) -- neither outcome rendered within the retry budget, so
        # whether a mutation happened cannot be determined from here. Fail
        # closed like every other post-click ambiguity in this module.
        return EducationDeleteResult(
            entry_id,
            kind,
            False,
            f"после клика 'Удалить' не подтверждён ни диалог, ни результат: {last_exc}",
            uncertain=True,
        )

    if dialog_appeared:
        # The second Magritte confirm dialog is up. Neither it nor its
        # buttons carry a data-qa (see _DELETE_CONFIRM_DIALOG comment above),
        # so the button is addressed by its exact visible text, scoped
        # strictly inside the dialog role to avoid matching unrelated
        # "Удалить" controls elsewhere on the page (e.g. the form's own
        # trigger, which is still mounted underneath the dialog). The
        # reservation already happened before the loop above (see comment
        # there) -- this click is the confirmed-destructive one for
        # kind="primary", so no further before_click() call belongs here.
        confirm_button = dialog.get_by_text(_DELETE_CONFIRM_BUTTON_TEXT, exact=True)
        if confirm_button.count() != 1:
            return EducationDeleteResult(
                entry_id,
                kind,
                False,
                "кнопка подтверждения во втором диалоге не найдена однозначно",
                uncertain=True,
            )
        try:
            confirm_button.first.click()
        except PlaywrightError as exc:
            return EducationDeleteResult(
                entry_id, kind, False, f"ошибка destructive-клика: {exc}", uncertain=True
            )
    # No dialog appeared -- the first click above was itself the destructive
    # one (matches the #809 report's unconfirmed claim for "additional").
    # The reservation already happened before the loop, so there is nothing
    # further to do here.

    # The positive signal is the delete button itself detaching -- hh.ru
    # tears down the whole form once the entry is gone, whether or not
    # page.url changes (live-confirmed it does NOT for the not-found case,
    # see _ENTRY_NOT_FOUND_TEXT above; the post-delete render was not
    # separately observed, so this code does not assume either way). Any
    # exception during this wait keeps the result uncertain -- the click may
    # have already reached hh.ru. The button re-check below (same
    # selector-absence check used pre-click above, generalized to "no longer
    # exactly one match") is the authoritative proof; detachment is only the
    # transition signal (same two-signal pattern as delete_resume.py).
    try:
        trigger.first.wait_for(state="detached", timeout=ENTRY_DELETE_VERIFY_TIMEOUT_MS)
    except PlaywrightError as exc:
        return EducationDeleteResult(
            entry_id,
            kind,
            False,
            f"не удалось подтвердить результат удаления: {exc}",
            uncertain=True,
        )
    if page.locator(_ENTRY_DELETE_TRIGGER[kind]).count() != 0:
        # The button detached (transition signal, checked above) but a fresh
        # count() still finds one -- most likely hh.ru re-rendered the same
        # form (SPA re-mount), so the delete did not actually go through.
        # page.url is not usable here: live-confirmed (see
        # _ENTRY_NOT_FOUND_TEXT) that it does not change even when the
        # entry is genuinely gone, so it cannot distinguish "still open" from
        # "closed".
        return EducationDeleteResult(
            entry_id,
            kind,
            False,
            "кнопка удаления всё ещё присутствует после клика",
            uncertain=True,
        )
    return EducationDeleteResult(entry_id, kind, True, "запись удалена; форма закрыта")

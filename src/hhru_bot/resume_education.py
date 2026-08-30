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
    HH_BASE_URL,
    PageStateIndeterminate,
    goto_hh,
    has_auth_cookie,
    has_login_form,
    labelled_field,
    open_hydrated_resume_editor,
    require_authenticated_page,
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
PRIMARY_ROUTE = re.compile(r"/profile/edit/primaryEducation(?=[?#]|$)")
ADDITIONAL_ROUTE = re.compile(r"/profile/edit/additionalEducation/[^/?#]+")
# The two education editors are DIFFERENT screens with different controls, so a
# single pair of buttons cannot serve both (live probe 2026-08-30):
#   primary    -> /profile/edit/primaryEducation      -> profile-layout-*
#   additional -> /resume/edit/<id>/additionalEducation -> resume-partial-edit-*
# Each candidate is count=0 on the other screen; keep them separate rather than
# guessing one shared id.
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
# The additional-education form carries NO data-qa on any of its inputs — they
# are bound only through aria-labelledby + <label> (Magritte, live probe
# 2026-08-30; every profile-education-additional-* candidate is count=0 there).
# The visible label is the only stable handle, addressed through
# browser.labelled_field, which requires one exact match and fails closed.
_ADDITIONAL_LABELS = {
    "institution": "Название",
    "organization": "Проводившая организация",
    "specialty": "Специализация",
    "year": "Год окончания",
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


def _field_locator(page, name: str, *, additional: bool):
    """Resolve one education field on whichever of the two forms is open.

    Both paths require exactly one match and raise otherwise, so a drifted
    selector or an ambiguous label stops the write instead of filling the
    wrong control.
    """
    if additional:
        return labelled_field(page, _ADDITIONAL_LABELS[name])
    locator = page.locator(_PRIMARY_FIELDS[name])
    if locator.count() != 1:
        raise PageStateIndeterminate(f"поле {_PRIMARY_FIELDS[name]} не найдено однозначно")
    return locator


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
                # The additional form exposes no data-qa on its inputs, so its
                # hydration marker is the editor's own save control (live probe
                # 2026-08-30: present on the rendered form, absent before it).
                editor_selector=(
                    ADDITIONAL_SAVE_BUTTON if additional else _PRIMARY_FIELDS["institution"]
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
                    locator = _field_locator(page, name, additional=additional)
                except PageStateIndeterminate as exc:
                    return EducationResult(
                        kind,
                        False,
                        str(exc),
                        uncertain=saved_count > 0,
                        saved=saved_count,
                    )
                locator.fill(value)
            if dry_run:
                page.locator(cancel_selector).first.click()
            else:
                save = page.locator(save_selector)
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
    # confirm dialog nor any network request follows. The first click is not
    # itself destructive (see below), so retrying it is safe -- this loop
    # bounds that wait instead of guessing a fixed sleep.
    click_attempts = 0
    dialog = page.locator(_DELETE_CONFIRM_DIALOG)
    trigger = page.locator(_ENTRY_DELETE_TRIGGER[kind])
    dialog_appeared = False
    last_exc: PlaywrightError | None = None
    while click_attempts < DELETE_CLICK_MAX_ATTEMPTS:
        click_attempts += 1
        # #809: the first click only OPENS the confirm dialog (kind="primary",
        # live-confirmed) or -- unconfirmed for "additional" -- may tear the
        # row down directly. It does not itself mutate hh.ru in the primary
        # case, so before_click() (the DurableMutationAttempt seam) is
        # deliberately NOT called here; it must fire immediately before
        # whichever click actually is the destructive one, exactly like
        # delete_resume.py's confirm step.
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
        # trigger, which is still mounted underneath the dialog).
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
            if before_click is not None:
                before_click()
            confirm_button.first.click()
        except PlaywrightError as exc:
            return EducationDeleteResult(
                entry_id, kind, False, f"ошибка destructive-клика: {exc}", uncertain=True
            )
    elif before_click is not None:
        # No dialog appeared -- the first click above was itself the
        # destructive one (matches the #809 report's unconfirmed claim for
        # "additional"). The reservation must still happen, just retroactively
        # relative to that click, since there is no later point to hook it to.
        before_click()

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

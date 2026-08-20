"""Read-only bulk questionnaire detection for ``probe``.

This module deliberately does not use the apply pipeline: it never fills a
field, invokes an LLM, writes history, or clicks the final submit button.

#433 cycle-review: session liveness is checked with ``require_authenticated_page``
(the same cookie+login-form contract used elsewhere in the project — see
``browser.has_login_form`` docstring), not a bare ``has_login_form`` call —
the absence of the login form alone does not prove an authenticated page
(the page may still be rendering, or the selector may have drifted).
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..ai.questions import Question, extract_questions
from ..browser import NotAuthenticated, goto_hh, require_authenticated_page
from ..search import VacancyCard
from .questions import detect_questions
from .steps import navigate_to_response_form, wait_apply_button

QUESTIONNAIRE = "questionnaire"
NO_QUESTIONNAIRE = "no_questionnaire"
UNKNOWN = "unknown"
UNAUTHENTICATED = "unauthenticated"

FAST_TIMEOUT_MS = 15_000
FAST_FORM_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class QuestionnaireScanResult:
    vacancy: VacancyCard
    status: str
    reason: str = ""
    questions: tuple[Question, ...] = ()
    total_bodies: int = 0
    retryable: bool = False


def _set_page_timeout(page: Page, timeout_ms: int) -> None:
    setter = getattr(page, "set_default_navigation_timeout", None)
    if callable(setter):
        setter(timeout_ms)


def scan_questionnaire(
    page: Page,
    vacancy: VacancyCard,
    *,
    timeout_ms: int = FAST_TIMEOUT_MS,
    form_timeout_ms: int = FAST_FORM_TIMEOUT_MS,
) -> QuestionnaireScanResult:
    """Inspect one vacancy on an already-open page, without external writes."""
    _set_page_timeout(page, timeout_ms)
    try:
        goto_hh(page, vacancy.url)
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return QuestionnaireScanResult(
            vacancy, UNKNOWN, f"страница вакансии недоступна: {exc}", retryable=True
        )

    try:
        require_authenticated_page(page)
    except NotAuthenticated as exc:
        return QuestionnaireScanResult(vacancy, UNAUTHENTICATED, str(exc))

    try:
        if not wait_apply_button(page, timeout_ms=form_timeout_ms):
            return QuestionnaireScanResult(
                vacancy, UNKNOWN, "кнопка отклика не подтверждена", retryable=True
            )
        form_state = navigate_to_response_form(
            page,
            vacancy.vacancy_id,
            navigation_timeout_ms=timeout_ms,
            form_timeout_ms=form_timeout_ms,
            dump_diagnostics=False,
        )
        if form_state is not True:
            return QuestionnaireScanResult(
                vacancy,
                UNKNOWN,
                str(form_state) if isinstance(form_state, str) else "форма отклика не подтверждена",
                retryable=form_state is False,
            )
        try:
            require_authenticated_page(page)
        except NotAuthenticated as exc:
            return QuestionnaireScanResult(vacancy, UNAUTHENTICATED, str(exc))
        detection = detect_questions(page)
        if detection.indeterminate:
            return QuestionnaireScanResult(vacancy, UNKNOWN, detection.reason, retryable=True)
        if not detection.has_questions:
            return QuestionnaireScanResult(vacancy, NO_QUESTIONNAIRE)
        questions, total_bodies = extract_questions(page)
        return QuestionnaireScanResult(
            vacancy,
            QUESTIONNAIRE,
            detection.reason,
            tuple(questions),
            total_bodies,
        )
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return QuestionnaireScanResult(vacancy, UNKNOWN, f"ошибка проверки: {exc}", retryable=True)

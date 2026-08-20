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
from ..external_forms.detect import normalize
from ..search import VacancyCard
from .dedup import check_already_responded
from .questions import detect_questions
from .steps import navigate_to_response_form, wait_apply_button

QUESTIONNAIRE = "questionnaire"
NO_QUESTIONNAIRE = "no_questionnaire"
UNKNOWN = "unknown"
UNAUTHENTICATED = "unauthenticated"
ALREADY_RESPONDED = "already_responded"

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


@dataclass(frozen=True)
class QuestionGroup:
    """One distinct question, merged across vacancies that ask it verbatim.

    #443 Этап 2: hh.ru repeats the same questionnaire question across many
    vacancies (screening questions are per-employer templates, not
    per-vacancy). Without grouping, a bulk scan's report is just a flat list
    of (vacancy, questions) pairs — every duplicate has to be spotted by eye,
    and there is no single place that says "this question appeared in N
    vacancies". Grouping is keyed on normalized text + kind + (for choice
    questions) the normalized option set: two questions with the same text
    but different answer choices are NOT the same question — merging them
    would make the group's `options` field lie about what a candidate can
    actually pick for any single vacancy in it.
    """

    text: str
    kind: str
    is_radio: bool
    options: tuple[str, ...]
    vacancy_ids: tuple[str, ...]


_GroupKey = tuple[str, str, bool, tuple[str, ...]]


def group_questions(results: list[QuestionnaireScanResult]) -> list[QuestionGroup]:
    """Group identical questions across scan results without losing the
    vacancy link (#443 acceptance: "повторяющиеся вопросы объединяются без
    потери связи с вакансиями"). Pure function — no browser, no I/O.
    """
    order: list[_GroupKey] = []
    # Original (non-normalized) display text/options are kept alongside the
    # normalized key on first sight, so the report can show what a candidate
    # actually reads on the page rather than the casefolded matching key.
    display: dict[_GroupKey, tuple[str, tuple[str, ...]]] = {}
    vacancy_ids: dict[_GroupKey, list[str]] = {}
    for result in results:
        if result.status != QUESTIONNAIRE:
            continue
        for question in result.questions:
            # #444 cycle-review: sort the normalized options for the key — the
            # key must match on the SET of options, not their on-page order
            # (the docstring's own contract). hh.ru can render the same
            # option set in a different order across vacancies; without
            # sorting, that alone would split one duplicate question into two
            # groups and undercount it. Display order (`question.options`) is
            # kept as-is in `display`, only the matching key is canonicalized.
            normalized_options = tuple(sorted(normalize(option) for option in question.options))
            key = (normalize(question.text), question.kind, question.is_radio, normalized_options)
            if key not in vacancy_ids:
                vacancy_ids[key] = []
                display[key] = (question.text, question.options)
                order.append(key)
            vacancy_id = result.vacancy.vacancy_id
            if vacancy_id not in vacancy_ids[key]:
                vacancy_ids[key].append(vacancy_id)
    return [
        QuestionGroup(
            text=display[key][0],
            kind=key[1],
            is_radio=key[2],
            options=display[key][1],
            vacancy_ids=tuple(vacancy_ids[key]),
        )
        for key in order
    ]


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
            # #433 cycle-review round 3: wait_apply_button() возвращает False и
            # для реального timeout/drift, и для штатного «уже откликались»
            # (она ждёт кнопку ИЛИ already-responded маркер одним локатором,
            # см. её докстринг) — эти случаи неразличимы без отдельной
            # проверки. Без неё обычная выдача с прежними откликами валит
            # весь bulk-скан как неподтверждённый. check_already_responded()
            # (read-only классификатор, уже используется в apply/probe.py по
            # тому же паттерну) отличает подтверждённый пропуск от таймаута.
            if reason := check_already_responded(page, vacancy):
                return QuestionnaireScanResult(vacancy, ALREADY_RESPONDED, reason)
            return QuestionnaireScanResult(
                vacancy, UNKNOWN, "кнопка отклика не подтверждена", retryable=True
            )
        form_state = navigate_to_response_form(
            page,
            vacancy.vacancy_id,
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
        if len(questions) != total_bodies:
            # #433 cycle-review round 3: extract_questions() может тихо
            # отбросить тело вопроса с нераспознанной структурой или
            # пустыми/неуникальными вариантами ответа (см. её докстринг и
            # тот же инвариант в apply/pipeline.py). Без этой проверки скан
            # репортил бы урезанный список вопросов как полную анкету.
            return QuestionnaireScanResult(
                vacancy,
                UNKNOWN,
                "анкета обнаружена, но распознана частично (расхождение эвристики и парсера)",
                retryable=False,
            )
        return QuestionnaireScanResult(
            vacancy,
            QUESTIONNAIRE,
            detection.reason,
            tuple(questions),
            total_bodies,
        )
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        return QuestionnaireScanResult(vacancy, UNKNOWN, f"ошибка проверки: {exc}", retryable=True)

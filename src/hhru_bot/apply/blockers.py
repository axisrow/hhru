"""Post-click HH.ru response blockers.

These checks deliberately live between the apply click and form processing.  A
missing response form is not enough evidence to classify the outcome: HH can
render a terminal popup instead.  Selectors are intentionally exact; generic
modal close buttons can close the response form itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from ..history import SKIP_REASONS
from ..selector_groups import vacancy_page

# CLAUDE.md п.4: клик по кнопке отклика запускает асинхронный React-рендер,
# поэтому сразу после него DOM ещё пуст.  Терминальные модалки проверяем только
# после короткого явного ожидания — иначе проверка систематически ничего не
# видит.  Таймаут намеренно мал: отсутствие модалки — штатный (частый) случай,
# и ждать её полный APPLY_TIMEOUT_MS на каждой вакансии нельзя.
BLOCKER_RENDER_TIMEOUT_MS = 1_500


@dataclass(frozen=True)
class PostClickBlocker:
    kind: str
    reason: str
    skip_reason: str | None = None
    stop_run: bool = False


class PostSubmitLimitExceeded(RuntimeError):
    """HH rendered the account response limit immediately after submit."""


def _visible(page: Page, selector: str) -> bool:
    try:
        locator = page.locator(selector).first
        return locator.is_visible()
    except (PlaywrightError, AttributeError):
        return False


def _text(page: Page, selector: str) -> str:
    try:
        return page.locator(selector).first.inner_text().lower()
    except (PlaywrightError, AttributeError):
        return ""


def _close_specific(page: Page, selector: str) -> None:
    try:
        locator = page.locator(selector).first
        if locator.is_visible():
            locator.click()
    except (PlaywrightError, AttributeError):
        return


def _wait_for_any_blocker(page: Page, timeout_ms: int) -> None:
    """Даёт модалке отрисоваться до строгих проверок видимости.

    Ждём первый попавшийся из терминальных якорей; отсутствие всех — штатный
    исход (форма отрисовалась нормально), поэтому таймаут проглатывается.
    """

    selector = ", ".join(
        (
            vacancy_page.VACANCY_RELOCATION_CONFIRM,
            vacancy_page.VACANCY_LIMIT_ERROR,
            vacancy_page.VACANCY_DIRECT_APPLICATION_CANCEL,
            vacancy_page.VACANCY_RESPONSE_REJECT_WARNING,
            vacancy_page.VACANCY_RESPONSE_ERROR,
            vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE,
        )
    )
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
    except (PlaywrightError, AttributeError):
        return


def handle_post_click_blockers(
    page: Page,
    *,
    allow_relocation: bool,
    render_timeout_ms: int = BLOCKER_RENDER_TIMEOUT_MS,
) -> PostClickBlocker | None:
    """Handle one post-click DOM state and return a terminal blocker if any.

    ``None`` means the caller may continue to form detection.  Similar-vacancy
    overlays are non-terminal and are closed using only their dedicated
    selectors.  Every terminal state is fail-closed and performs no submit.
    """

    _wait_for_any_blocker(page, render_timeout_ms)

    if _visible(page, vacancy_page.VACANCY_RELOCATION_CONFIRM):
        if allow_relocation:
            _close_specific(page, vacancy_page.VACANCY_RELOCATION_CONFIRM)
        else:
            return PostClickBlocker(
                "relocation_not_allowed",
                "HH запросил подтверждение готовности к переезду; "
                "подтверждение отключено настройками проекта",
                SKIP_REASONS.RELOCATION_NOT_ALLOWED,
            )

    if _visible(page, vacancy_page.VACANCY_LIMIT_ERROR):
        return PostClickBlocker(
            "limit_exceeded",
            "HH.ru сообщил об исчерпанном лимите откликов; текущий прогон остановлен",
            stop_run=True,
        )

    if _visible(page, vacancy_page.VACANCY_DIRECT_APPLICATION_CANCEL):
        alert_text = _text(page, vacancy_page.VACANCY_DIRECT_APPLICATION_ALERT)
        if any(marker in alert_text for marker in ("прямым откликом", "сайте работодателя")):
            return PostClickBlocker(
                "direct_application",
                "вакансия требует отклика на сайте работодателя",
                SKIP_REASONS.DIRECT_APPLICATION,
            )

    # В отличие от direct-application, здесь текстовый гейт не нужен: оба
    # data-qa специфичны для отклика, тогда как ``magritte-alert`` выше —
    # generic-контейнер дизайн-системы и без проверки текста дал бы ложный скип.
    if _visible(page, vacancy_page.VACANCY_RESPONSE_REJECT_WARNING) or _visible(
        page, vacancy_page.VACANCY_RESPONSE_ERROR
    ):
        return PostClickBlocker(
            "response_rejected",
            "HH.ru показал предупреждение или ошибку отклика",
            SKIP_REASONS.RESPONSE_REJECTED,
        )

    # This popup only covers the form; close it and let normal form detection
    # continue.  Never broaden this to a generic close button.
    _close_specific(page, vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE)
    _close_specific(page, 'button:has-text("Не сейчас")')
    return None


def raise_if_post_submit_limit(page: Page) -> None:
    """Raise when HH shows the response limit after the submit click."""
    if _visible(page, vacancy_page.VACANCY_LIMIT_ERROR):
        raise PostSubmitLimitExceeded(
            "HH.ru сообщил об исчерпанном лимите откликов после submit; текущий прогон остановлен"
        )

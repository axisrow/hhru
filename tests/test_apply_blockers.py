"""Fixture-level tests for HH post-click response blockers."""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot.apply.blockers import (
    PostSubmitLimitExceeded,
    handle_post_click_blockers,
    raise_if_post_submit_limit,
)
from hhru_bot.history import SKIP_REASONS
from hhru_bot.selector_groups import vacancy_page

pytestmark = pytest.mark.integration


class _Locator:
    def __init__(self, page: _Page, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def is_visible(self) -> bool:
        return self.page.elements.get(self.selector, (False, ""))[0]

    def inner_text(self) -> str:
        return self.page.elements.get(self.selector, (False, ""))[1]

    def wait_for(self, *, state: str = "visible", timeout: float = 0) -> None:
        self.page.waited.append(self.selector)
        # Совокупный селектор ждёт первый видимый якорь; в фикстурах DOM
        # синхронный, поэтому достаточно проверить любую из частей.
        if not any(
            self.page.elements.get(part.strip(), (False, ""))[0]
            for part in self.selector.split(",")
        ):
            raise PlaywrightError("locator.wait_for: Timeout exceeded")

    def click(self) -> None:
        self.page.clicked.append(self.selector)
        self.page.elements[self.selector] = (False, self.inner_text())


class _Page:
    def __init__(self, *elements: tuple[str, str]) -> None:
        self.elements = {selector: (True, text) for selector, text in elements}
        self.clicked: list[str] = []
        self.waited: list[str] = []

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)


def test_relocation_is_skipped_by_default_without_click():
    page = _Page((vacancy_page.VACANCY_RELOCATION_CONFIRM, "Готовы к переезду?"))

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is not None
    assert result.skip_reason == SKIP_REASONS.RELOCATION_NOT_ALLOWED
    assert page.clicked == []


def test_relocation_can_be_confirmed_only_by_explicit_policy():
    page = _Page((vacancy_page.VACANCY_RELOCATION_CONFIRM, "Готовы к переезду?"))

    result = handle_post_click_blockers(page, allow_relocation=True)

    assert result is None
    assert page.clicked == [vacancy_page.VACANCY_RELOCATION_CONFIRM]


def test_similar_popup_is_closed_without_becoming_terminal():
    page = _Page((vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE, "Не сейчас"))

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is None
    assert page.clicked == [vacancy_page.VACANCY_SIMILAR_VACANCIES_CLOSE]


def test_direct_application_requires_alert_text():
    page = _Page(
        (vacancy_page.VACANCY_DIRECT_APPLICATION_CANCEL, "Отменить"),
        (vacancy_page.VACANCY_DIRECT_APPLICATION_ALERT, "Вакансия с прямым откликом"),
    )

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is not None
    assert result.skip_reason == SKIP_REASONS.DIRECT_APPLICATION


def test_limit_stops_run_before_submit():
    page = _Page((vacancy_page.VACANCY_LIMIT_ERROR, "Лимит откликов"))

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is not None
    assert result.kind == "limit_exceeded"
    assert result.stop_run is True


def test_limit_is_checked_again_after_submit():
    page = _Page((vacancy_page.VACANCY_LIMIT_ERROR, "Лимит откликов"))

    with pytest.raises(PostSubmitLimitExceeded):
        raise_if_post_submit_limit(page)


def test_response_warning_is_a_terminal_skip():
    page = _Page((vacancy_page.VACANCY_RESPONSE_REJECT_WARNING, "Скорее всего, будет отказ"))

    result = handle_post_click_blockers(page, allow_relocation=False)

    assert result is not None
    assert result.skip_reason == SKIP_REASONS.RESPONSE_REJECTED


def test_blockers_wait_for_render_before_strict_checks():
    # CLAUDE.md п.4: без явного ожидания проверка читает ещё не отрисованный
    # React-DOM и систематически не видит модалку.
    page = _Page((vacancy_page.VACANCY_LIMIT_ERROR, "Лимит откликов"))

    handle_post_click_blockers(page, allow_relocation=False)

    assert page.waited, "терминальные проверки выполнены без ожидания рендера"

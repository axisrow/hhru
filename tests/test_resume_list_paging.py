"""Многостраничное чтение /applicant/my_resumes (#1133)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot import resume_list_pages
from hhru_bot.selector_groups.resume_list import RESUME_LIST_CARD
from hhru_bot.selector_groups.search_page import PAGINATION_NEXT

pytestmark = pytest.mark.integration

RESUME_ID = "b" * 38


class _Locator:
    def __init__(self, page, selector, count):
        self._page = page
        self._selector = selector
        self._count = count

    @property
    def first(self):
        return self

    def count(self):
        # При смене страницы фейка перерисовывается: счётчик берём заново.
        return self._page._count_for(self._selector)

    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self._page._count_for(self._selector) == 0:
            raise PlaywrightError("not attached")

    def click(self, *, timeout=None):  # noqa: ARG002
        assert self._selector == PAGINATION_NEXT
        self._page.clicks += 1
        self._page.page_index += 1


class _PagedPage:
    """Список из N страниц: карточки искомого резюме — только на page_index."""

    def __init__(self, pages, other_cards=3):
        self._pages = pages  # list[dict]: {"target": bool, "pager_next": bool}
        self._other_cards = other_cards
        self.page_index = 0
        self.clicks = 0
        self.url = "https://hh.ru/applicant/my_resumes"
        self.broken_click = False

    def _count_for(self, selector):
        if selector == PAGINATION_NEXT:
            return 1 if self._pages[self.page_index]["pager_next"] else 0
        if selector.startswith(RESUME_LIST_CARD) and ":has(" in selector:
            # identity-локатор (resume_card_locator)
            return 1 if self._pages[self.page_index]["target"] else 0
        if selector == RESUME_LIST_CARD:
            return self._other_cards
        raise AssertionError(selector)

    def locator(self, selector):
        return _Locator(self, selector, self._count_for(selector))

    def wait_for_url(self, predicate, *, wait_until, timeout):  # noqa: ARG002
        if self.broken_click:
            raise PlaywrightError("navigation did not confirm")
        # Реальная смена URL подтверждает переход (#179: wait_until="commit").
        self.url = f"https://hh.ru/applicant/my_resumes?page={self.page_index + 1}"


def test_first_page_hit_does_not_touch_pager():
    page = _PagedPage([{"target": True, "pager_next": False}])
    card, error = resume_list_pages.find_resume_card(page, RESUME_ID, wait_attached_ms=1)
    assert card is not None and error == ""
    assert page.clicks == 0
    assert page.page_index == 0


def test_card_on_second_page_is_found_through_pager():
    page = _PagedPage(
        [
            {"target": False, "pager_next": True},
            {"target": True, "pager_next": False},
        ]
    )
    card, error = resume_list_pages.find_resume_card(page, RESUME_ID, wait_attached_ms=1)
    assert card is not None and error == ""
    assert page.page_index == 1


def test_absent_on_last_page_is_clean_none():
    page = _PagedPage([{"target": False, "pager_next": False}])
    card, error = resume_list_pages.find_resume_card(page, RESUME_ID, wait_attached_ms=1)
    assert card is None and error == ""


def test_unconfirmed_page_turn_is_not_reported_as_absent():
    page = _PagedPage(
        [
            {"target": False, "pager_next": True},
            {"target": True, "pager_next": False},
        ]
    )
    page.broken_click = True
    card, error = resume_list_pages.find_resume_card(page, RESUME_ID, wait_attached_ms=1)
    assert card is None
    assert "не дочитан" in error


def test_page_cap_is_fail_closed():
    pages = [
        {"target": False, "pager_next": True} for _ in range(resume_list_pages.MAX_LIST_PAGES + 1)
    ]
    page = _PagedPage(pages)
    card, error = resume_list_pages.find_resume_card(page, RESUME_ID, wait_attached_ms=1)
    assert card is None
    assert "не дочитан" in error
    assert page.page_index == resume_list_pages.MAX_LIST_PAGES


def test_collect_pages_merges_and_stops_on_last():
    seen_pages = []
    page = _PagedPage(
        [
            {"target": False, "pager_next": True},
            {"target": False, "pager_next": False},
        ]
    )

    def collect(pg):
        seen_pages.append(pg.page_index)
        return [f"card-p{pg.page_index}"]

    cards = resume_list_pages.collect_resume_cards_over_pages(page, collect)
    assert cards == ["card-p0", "card-p1"]
    assert seen_pages == [0, 1]


def test_collect_raises_on_unconfirmed_turn():
    page = _PagedPage(
        [
            {"target": False, "pager_next": True},
            {"target": False, "pager_next": False},
        ]
    )
    page.broken_click = True

    def collect(pg):  # noqa: ARG001
        return ["card-p0"]

    with pytest.raises(RuntimeError, match="не дочитан"):
        resume_list_pages.collect_resume_cards_over_pages(page, collect)

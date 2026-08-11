"""Регрессии #141: pagination negotiations не должна ложно завершать обход."""

from __future__ import annotations

import pytest

import hhru_bot.responses as responses
from hhru_bot.selector_groups import negotiations as ns


class _Locator:
    def __init__(self, labels: list[str], delayed_labels: list[str] | None = None):
        self.labels = labels
        self.delayed_labels = delayed_labels
        self.wait_calls: list[tuple[str, int]] = []

    def count(self):
        return len(self.labels)

    @property
    def first(self):
        return self

    def wait_for(self, *, state: str, timeout: int):
        self.wait_calls.append((state, timeout))
        if self.delayed_labels:
            self.labels.extend(self.delayed_labels)
            return
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        raise PlaywrightTimeoutError("pagination did not render")

    def nth(self, index: int):
        return _Text(self.labels[index])


class _Text:
    def __init__(self, text: str):
        self.text = text

    def inner_text(self):
        return self.text


class _Page:
    def __init__(
        self,
        labels: list[str],
        delayed_labels: list[str] | None = None,
        has_pagination_block: bool = True,
    ):
        self.next = _Locator([])
        self.block = _Locator(["block"] if has_pagination_block else [])
        self.pages = _Locator(labels, delayed_labels)

    def locator(self, selector: str):
        if selector == ns.NEGOTIATIONS_PAGINATION_NEXT:
            return self.next
        if selector == ns.NEGOTIATIONS_PAGINATION_BLOCK:
            return self.block
        if selector == ns.NEGOTIATIONS_PAGINATION_PAGE:
            return self.pages
        raise AssertionError(f"unexpected selector: {selector}")


class _ResponsesPage:
    def __init__(self, cards: list[object], delayed_cards: list[object] | None = None):
        self.url = "https://hh.ru/applicant/negotiations"
        self.cards = _DelayedCardsLocator(cards, delayed_cards)

    def locator(self, selector: str):
        assert selector == ns.NEGOTIATION_ITEM
        return self.cards


class _DelayedCardsLocator:
    def __init__(self, cards: list[object], delayed_cards: list[object] | None = None):
        self.cards = cards
        self.delayed_cards = delayed_cards

    def count(self):
        return len(self.cards)

    @property
    def first(self):
        return self

    def wait_for(self, *, state: str, timeout: int):  # noqa: ARG002
        if self.delayed_cards:
            self.cards.extend(self.delayed_cards)
            return
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        raise PlaywrightTimeoutError("response cards did not render")

    def nth(self, index: int):
        return self.cards[index]


def test_responses_pagination_waits_for_delayed_page_markers():
    page = _Page([], delayed_labels=["1", "2"])

    assert responses._has_next_page(page, 0) is True
    assert page.pages.wait_calls == [("attached", responses.RENDER_TIMEOUT_MS)]


def test_responses_pagination_timeout_is_not_last_page():
    page = _Page([])

    with pytest.raises(responses.ResponsesIndeterminate, match="не подтверждена"):
        responses._has_next_page(page, 0)


def test_responses_without_pagination_block_is_confirmed_single_page():
    page = _Page([], has_pagination_block=False)

    assert responses._has_next_page(page, 0) is False


def test_fetch_responses_waits_for_delayed_cards(monkeypatch):
    page = _ResponsesPage([], delayed_cards=[object()])
    expected = responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.READ)
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: expected)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    assert responses.fetch_responses(page, max_pages=1) == [expected]


def test_fetch_responses_timeout_is_not_empty(monkeypatch):
    page = _ResponsesPage([])
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)

    with pytest.raises(responses.ResponsesIndeterminate, match="не подтверждён"):
        responses.fetch_responses(page, max_pages=1)

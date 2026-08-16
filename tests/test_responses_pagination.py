"""Регрессии #141: pagination negotiations не должна ложно завершать обход."""

from __future__ import annotations

import pytest

import hhru_bot.responses as responses
from hhru_bot.browser import LOGIN_FORM
from hhru_bot.selector_groups import negotiations as ns


class _Locator:
    def __init__(
        self,
        labels: list[str],
        delayed_labels: list[str] | None = None,
        disappears_after_wait: bool = False,
    ):
        self.labels = labels
        self.delayed_labels = delayed_labels
        self.disappears_after_wait = disappears_after_wait
        self.wait_calls: list[tuple[str, int]] = []

    def count(self):
        return len(self.labels)

    @property
    def first(self):
        return self

    def wait_for(self, *, state: str, timeout: int):
        self.wait_calls.append((state, timeout))
        if self.disappears_after_wait:
            return
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
        disappears_after_wait: bool = False,
    ):
        self.next = _Locator([])
        self.block = _Locator(["block"] if has_pagination_block else [])
        self.pages = _Locator(labels, delayed_labels, disappears_after_wait)

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
        self.ssr_html = ""
        self.cards = _DelayedCardsLocator(cards, delayed_cards)

    def content(self):
        return self.ssr_html

    def locator(self, selector: str):
        if selector == LOGIN_FORM:
            return _Locator([])
        assert selector == ns.NEGOTIATION_ITEM
        return self.cards


class _ResponseCard:
    def __init__(self, *, has_chat: bool):
        self.has_chat = has_chat

    def locator(self, selector: str):
        assert selector == ns.NEGOTIATION_CHAT_LINK
        return _Locator(["chat"] if self.has_chat else [])


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


def test_responses_pagination_marker_disappearing_after_wait_is_not_last_page():
    page = _Page([], disappears_after_wait=True)

    with pytest.raises(responses.ResponsesIndeterminate, match="исчез после ожидания"):
        responses._has_next_page(page, 0)


def test_responses_without_pagination_block_is_confirmed_single_page():
    page = _Page([], has_pagination_block=False)

    assert responses._has_next_page(page, 0) is False


def test_fetch_responses_waits_for_delayed_cards(monkeypatch):
    page = _ResponsesPage([], delayed_cards=[object()])
    expected = responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.READ)
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: expected)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    assert responses.fetch_responses(page, max_pages=1) == [expected]


def test_fetch_responses_recovers_topic_from_ssr_state(monkeypatch):
    page = _ResponsesPage([_ResponseCard(has_chat=True)])
    page.ssr_html = """
    <template id="HH-Lux-InitialState">
      {"applicantNegotiations":{"topicList":[
        {"id":123,"chatId":456,"vacancyId":42}
      ]}}
    </template>
    """
    expected = responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.READ)
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: expected)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    assert responses.fetch_responses(page, max_pages=1) == [
        responses.ResponseItem(
            vacancy_id="42",
            status=responses.ResponseStatus.READ,
            topic="123",
            chat_url="https://chatik.hh.ru/chat/456",
        )
    ]


def test_fetch_responses_does_not_consume_ssr_ref_for_no_chat_card(monkeypatch):
    cards = [_ResponseCard(has_chat=False), _ResponseCard(has_chat=True)]
    page = _ResponsesPage(cards)
    page.ssr_html = """
    <template id="HH-Lux-InitialState">
      {"applicantNegotiations":{"topicList":[
        {"id":123,"chatId":456,"vacancyId":42}
      ]}}
    </template>
    """
    items = iter(
        [
            responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.DISCARD),
            responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.READ),
        ]
    )
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: next(items))
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    result = responses.fetch_responses(page, max_pages=1)
    assert result[0].topic is None
    assert result[1].topic == "123"


def test_fetch_responses_fails_closed_for_ambiguous_ssr_refs(monkeypatch):
    page = _ResponsesPage([_ResponseCard(has_chat=True)])
    page.ssr_html = """
    <template id="HH-Lux-InitialState">
      {"applicantNegotiations":{"topicList":[
        {"id":123,"chatId":456,"vacancyId":42},
        {"id":124,"chatId":457,"vacancyId":42}
      ]}}
    </template>
    """
    expected = responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.READ)
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: expected)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="однозначного"):
        responses.fetch_responses(page, max_pages=1)


def test_fetch_responses_fails_closed_for_chat_without_ssr_ref(monkeypatch):
    page = _ResponsesPage([_ResponseCard(has_chat=True)])
    expected = responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.READ)
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: expected)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="однозначного"):
        responses.fetch_responses(page, max_pages=1)


def test_fetch_responses_fails_closed_for_multi_card_same_vacancy_pairing(monkeypatch):
    """#185 follow-up: equal counts alone don't prove DOM/SSR order correspondence.

    Two chat-having cards for the same vacancy plus two SSR refs pass the
    count-equality guard, but nothing verifies the cards are paired to the
    *right* SSR entries — positional (FIFO) pairing could silently attach the
    wrong topic/chat_url. Must fail closed instead of guessing.
    """
    page = _ResponsesPage([_ResponseCard(has_chat=True), _ResponseCard(has_chat=True)])
    page.ssr_html = """
    <template id="HH-Lux-InitialState">
      {"applicantNegotiations":{"topicList":[
        {"id":123,"chatId":456,"vacancyId":42},
        {"id":124,"chatId":457,"vacancyId":42}
      ]}}
    </template>
    """
    items = iter(
        [
            responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.READ),
            responses.ResponseItem(vacancy_id="42", status=responses.ResponseStatus.INVITATION),
        ]
    )
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: next(items))
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="однозначного"):
        responses.fetch_responses(page, max_pages=1)


def test_fetch_responses_timeout_preserves_empty_inbox_contract(monkeypatch):
    page = _ResponsesPage([])
    monkeypatch.setattr(responses, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)

    assert responses.fetch_responses(page, max_pages=1) == []


def test_fetch_responses_timeout_on_confirmed_later_page_is_indeterminate(monkeypatch):
    """A confirmed next page must not be silently treated as an empty page."""
    page = _ResponsesPage([])
    visits = 0

    def goto(_page, _url):
        nonlocal visits
        visits += 1
        if visits == 2:
            page.cards.cards = []

    monkeypatch.setattr(responses, "goto_hh", goto)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "_has_next_page", lambda _page, page_num: page_num == 0)

    # The first page contains a card and confirms that page 1 exists.  The
    # second page then times out before any card is attached.
    page.cards.cards = [object()]
    monkeypatch.setattr(responses, "parse_response_card", lambda card: None)

    with pytest.raises(responses.ResponsesIndeterminate, match="страницы 1"):
        responses.fetch_responses(page, max_pages=2)

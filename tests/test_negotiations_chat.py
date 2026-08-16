import logging
from typing import cast

from playwright.sync_api import Page

from hhru_bot.negotiations_chat import (
    CHAT_MESSAGE_MY_MARKER,
    ChatMessage,
    extract_external_test_link,
    needs_reply,
    read_chat,
    wait_reply_confirmation,
)


def test_needs_reply_when_last_message_is_from_employer():
    decision = needs_reply(ChatMessage("employer", "message-1"))
    assert decision.should_reply is True
    assert decision.reason == "last_message_from_employer"


def test_needs_reply_skips_our_last_message():
    decision = needs_reply(ChatMessage("me", "message-1"))
    assert decision.should_reply is False
    assert decision.reason == "last_message_from_us"


def test_needs_reply_is_fail_closed_for_empty_chat():
    assert needs_reply(None).reason == "empty_chat"


def test_needs_reply_is_fail_closed_for_unknown_author_or_marker():
    assert needs_reply(ChatMessage(None, "message-1")).should_reply is False
    assert needs_reply(ChatMessage("employer", None)).reason == "inbound_marker_unknown"


def test_read_chat_logs_and_fails_closed_for_unmapped_topic(caplog):
    # An unmapped topic returns before ``page`` is touched, so a typed stand-in
    # is enough — no real Playwright Page is needed for this branch.
    fake_page = cast(Page, object())
    with caplog.at_level(logging.WARNING, logger="hhru_bot.negotiations_chat"):
        result = read_chat(fake_page, topic="unknown-topic", topic_to_chat_id={})

    assert result is None
    assert any("unknown-topic" in record.message for record in caplog.records)


def test_extracts_external_link_from_message():
    assert extract_external_test_link("Пройдите тест: https://yay-tech.ru/test") == (
        "https://yay-tech.ru/test"
    )


def test_message_without_link_returns_none():
    assert extract_external_test_link("Добрый день, готовы обсудить вакансию") is None


def test_returns_first_external_link_when_message_has_several():
    assert extract_external_test_link("https://example.com/a и https://other.test/b") == (
        "https://example.com/a"
    )


def test_hh_links_are_not_external():
    assert extract_external_test_link("https://hh.ru/vacancy/1 https://cdn.hhcdn.ru/file") is None


def test_trailing_sentence_punctuation_is_not_part_of_url():
    assert extract_external_test_link("Тест: https://example.com/test.") == (
        "https://example.com/test"
    )


# --- wait_reply_confirmation (Codex review, PR #198) ------------------------
#
# send_reply_current() only clicks the send button; it never confirms the
# server actually accepted the message. wait_reply_confirmation() is the
# positive-signal poll that closes that gap: it waits for the last chat
# message to become "ours" (same CHAT_MESSAGE_MY_MARKER used by
# read_last_message), mirroring apply/success.wait_success_confirmation's
# positive-only, timeout-is-false-negative contract.


class _FakeMessage:
    def __init__(self, is_own: bool):
        self._is_own = is_own

    def evaluate(self, _script, marker):
        assert marker == CHAT_MESSAGE_MY_MARKER
        return self._is_own


class _FakeMessages:
    """Imitates a Playwright Locator over CHAT_MESSAGE_TEXT: count()/nth()."""

    def __init__(self, authors: list[bool]):
        self._authors = authors

    def count(self) -> int:
        return len(self._authors)

    def nth(self, index: int) -> _FakeMessage:
        return _FakeMessage(self._authors[index])


class _FakeChatPage:
    """Minimal Page stand-in: only locator()/wait_for_timeout()/url are used."""

    def __init__(self, authors: list[bool] | None = None, *, late_after: int | None = None):
        self._authors = authors if authors is not None else []
        self._late_after = late_after
        self._polls = 0
        self.url = "https://hh.ru/chat/1"

    def wait_for_timeout(self, _ms: float) -> None:
        return None

    def locator(self, _selector: str) -> _FakeMessages:
        self._polls += 1
        if self._late_after is not None and self._polls <= self._late_after:
            return _FakeMessages([False])
        return _FakeMessages(self._authors)


def test_confirms_when_last_message_becomes_ours():
    page = _FakeChatPage(authors=[True])
    assert wait_reply_confirmation(cast(Page, page)) is True


def test_not_confirmed_when_last_message_is_still_employers():
    """The click may have failed silently (server rejection, network error);
    the last message staying the employer's is not a delivery signal."""
    page = _FakeChatPage(authors=[False])
    assert wait_reply_confirmation(cast(Page, page), timeout_ms=0) is False


def test_not_confirmed_on_empty_chat():
    page = _FakeChatPage(authors=[])
    assert wait_reply_confirmation(cast(Page, page), timeout_ms=0) is False


def test_confirms_via_late_async_render():
    """hh.ru may render the sent message asynchronously; the poll loop must
    catch it within the timeout rather than judging only the first read."""
    page = _FakeChatPage(authors=[True], late_after=1)
    assert wait_reply_confirmation(cast(Page, page), timeout_ms=2000) is True


def test_confirmation_timeout_logs_page_url(caplog):
    page = _FakeChatPage(authors=[False])
    page.url = "https://hh.ru/chat/42"
    with caplog.at_level(logging.WARNING, logger="hhru_bot.negotiations_chat"):
        result = wait_reply_confirmation(cast(Page, page), timeout_ms=0)
    assert result is False
    assert any("https://hh.ru/chat/42" in record.message for record in caplog.records)

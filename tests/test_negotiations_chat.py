import logging
from typing import cast

from playwright.sync_api import Page

from hhru_bot.negotiations_chat import (
    ChatMessage,
    extract_external_test_link,
    needs_reply,
    read_chat,
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

from hhru_bot.negotiations_chat import ChatMessage, extract_external_test_link, needs_reply


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

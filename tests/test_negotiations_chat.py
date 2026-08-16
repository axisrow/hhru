from hhru_bot.negotiations_chat import extract_external_test_link


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

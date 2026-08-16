from hhru_bot.negotiations_probe import chat_url, parse_initial_state, topic_refs


def test_topic_refs_read_ssr_state_without_page_actions():
    html = """
    <template id="HH-Lux-InitialState">
      {"applicantNegotiations":{"topicList":[{"id":123,"chatId":456,"vacancyId":789}]}}
    </template>
    """
    assert topic_refs(html)[0].topic_id == "123"
    assert topic_refs(html)[0].chat_id == "456"
    assert parse_initial_state(html)["applicantNegotiations"]["topicList"]


def test_chat_url_matches_hh_open_chat_route():
    assert chat_url("456") == "https://chatik.hh.ru/chat/456"
    assert chat_url("456", "https://chatik.example/") == "https://chatik.example/chat/456"

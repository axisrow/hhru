import json

import pytest

from hhru_bot.negotiations_probe import (
    chat_url,
    paginated_topic_refs,
    parse_initial_state,
    topic_refs,
)
from hhru_bot.responses import NotAuthenticated

pytestmark = pytest.mark.integration


def test_topic_refs_read_ssr_state_without_page_actions():
    html = """
    <template id="HH-Lux-InitialState">
      {"applicantNegotiations":{"topicList":[{"id":123,"chatId":456,"vacancyId":789}]}}
    </template>
    """
    assert topic_refs(html)[0].topic_id == "123"
    assert topic_refs(html)[0].chat_id == "456"
    assert parse_initial_state(html)["applicantNegotiations"]["topicList"]


def test_topic_refs_read_resume_id_from_ssr():
    """#200: SSR отдаёт resumeId — привязка чата к резюме существует.

    Регрессия на само заблуждение: посылка #55 §1.3 «/applicant/negotiations не
    даёт достоверной привязки чата к резюме» опровергнута живой проверкой
    2026-08-16 (поле есть у 7/7 переписок). Фикстура повторяет реальную форму
    записи, включая числовой (не строковый) resumeId.
    """
    html = """
    <template id="HH-Lux-InitialState">
      {"applicantNegotiations":{"topicList":[
        {"id":5503507503,"chatId":5552659058,"vacancyId":135481754,"resumeId":96223331}
      ]}}
    </template>
    """
    ref = topic_refs(html)[0]
    assert ref.resume_id == "96223331"
    assert ref.vacancy_id == "135481754"


def test_topic_refs_keep_mapping_when_resume_id_absent():
    """Отсутствие resumeId — fail-open: маппинг topic→chat уцелел.

    responses/reply-employers зависят от topic→chat; дрейф аналитического поля
    не должен ронять их запись.
    """
    html = """
    <template id="HH-Lux-InitialState">
      {"applicantNegotiations":{"topicList":[{"id":123,"chatId":456,"vacancyId":789}]}}
    </template>
    """
    ref = topic_refs(html)[0]
    assert ref.resume_id is None
    assert (ref.topic_id, ref.chat_id) == ("123", "456")


def test_paginated_topic_refs_collects_all_pages(monkeypatch):
    class Page:
        def __init__(self):
            self.page_num = 0

        def content(self):
            state = {
                "applicantNegotiations": {
                    "topicList": [
                        {
                            "id": self.page_num + 1,
                            "chatId": self.page_num + 11,
                            "vacancyId": self.page_num + 21,
                        }
                    ]
                }
            }
            return '<template id="HH-Lux-InitialState">' + json.dumps(state) + "</template>"

    page = Page()
    urls = []

    def goto(_page, url):
        urls.append(url)
        page.page_num = len(urls) - 1

    monkeypatch.setattr("hhru_bot.browser.goto_hh", goto)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _page: False)
    monkeypatch.setattr("hhru_bot.responses._has_next_page", lambda _page, n: n < 2)

    refs = paginated_topic_refs(page, max_pages=3)

    assert [ref.topic_id for ref in refs] == ["1", "2", "3"]
    assert urls == [
        "https://hh.ru/applicant/negotiations",
        "https://hh.ru/applicant/negotiations?page=1",
        "https://hh.ru/applicant/negotiations?page=2",
    ]


# --- Codex review (#201): expired session must not surface as a raw ValueError


def test_paginated_topic_refs_raises_not_authenticated_without_cookie(monkeypatch):
    """No hhtoken cookie must fail as NotAuthenticated, not crash inside
    topic_refs() with a ValueError about a missing SSR template — same
    contract fetch_responses() already enforces.
    """

    class Page:
        def content(self):
            raise AssertionError("must not read SSR state before the auth check")

    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda page, url: None)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: False)

    with pytest.raises(NotAuthenticated):
        paginated_topic_refs(Page(), max_pages=1)


def test_paginated_topic_refs_raises_not_authenticated_on_login_form(monkeypatch):
    """A server-rendered login form (rejected/stale hhtoken) must also fail
    as NotAuthenticated, not as topic_refs()'s missing-SSR-template ValueError.
    """

    class Page:
        def content(self):
            raise AssertionError("must not read SSR state before the auth check")

    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda page, url: None)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _page: True)

    with pytest.raises(NotAuthenticated):
        paginated_topic_refs(Page(), max_pages=1)


def test_chat_url_matches_hh_open_chat_route():
    assert chat_url("456") == "https://chatik.hh.ru/chat/456"
    assert chat_url("456", "https://chatik.example/") == "https://chatik.example/chat/456"

import json

import pytest

from hhru_bot.negotiations_probe import (
    chat_url,
    paginated_topic_and_remindable_refs,
    paginated_topic_refs,
    parse_initial_state,
    topic_refs,
)
from hhru_bot.responses import NotAuthenticated, ResponsesIndeterminate

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

    refs = paginated_topic_refs(page, max_pages=3)

    assert [ref.topic_id for ref in refs] == ["1", "2", "3"]
    assert urls == [
        "https://hh.ru/applicant/negotiations",
        "https://hh.ru/applicant/negotiations?page=1",
        "https://hh.ru/applicant/negotiations?page=2",
    ]


def _state_html(topic_ids):
    state = {
        "applicantNegotiations": {
            "topicList": [
                {"id": tid, "chatId": tid + 100, "vacancyId": tid + 200} for tid in topic_ids
            ]
        }
    }
    return '<template id="HH-Lux-InitialState">' + json.dumps(state) + "</template>"


def test_paginated_topic_refs_walks_pagerless_lazy_tail(monkeypatch):
    """Дрейф 2026-09-09: hh.ru убрал пейджер из UI (хвост списка — lazy-скролл),
    серверный ?page=N жив. Отсутствие пейджера больше не «страница одна»:
    обход продолжает GET-ить следующую страницу, пока та приносит НОВЫЕ
    топики; страница-повтор/пустышка — конец. Живой факт: «Все 30» при 20
    карточках SSR, ?page=1 отдал оставшиеся 10, ?page=2 — повтор."""

    pages = {
        0: [1, 2, 3],
        1: [4, 5],
        2: [4, 5],  # сервер за последней страницей повторяет хвост
    }

    class Page:
        def __init__(self):
            self.page_num = 0

        def content(self):
            return _state_html(pages[self.page_num])

    page = Page()
    urls = []

    def goto(_page, url):
        urls.append(url)
        page.page_num = len(urls) - 1

    monkeypatch.setattr("hhru_bot.browser.goto_hh", goto)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _page: False)

    refs = paginated_topic_refs(page, max_pages=5)

    assert [ref.topic_id for ref in refs] == ["1", "2", "3", "4", "5"]
    assert urls[-1] == "https://hh.ru/applicant/negotiations?page=2"


def test_paginated_topic_refs_survives_server_ignoring_page_param(monkeypatch):
    """Сервер, игнорирующий ?page (отдаёт первую страницу на любой номер), —
    ноль новых топиков на page=1 → стоп, без дублей и без вечного цикла."""

    class Page:
        def __init__(self):
            self.page_num = 0

        def content(self):
            return _state_html([1, 2])

    page = Page()
    urls = []

    def goto(_page, url):
        urls.append(url)
        page.page_num = len(urls) - 1

    monkeypatch.setattr("hhru_bot.browser.goto_hh", goto)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _page: False)

    refs = paginated_topic_refs(page, max_pages=5)

    assert [ref.topic_id for ref in refs] == ["1", "2"]
    assert urls == [
        "https://hh.ru/applicant/negotiations",
        "https://hh.ru/applicant/negotiations?page=1",
    ]


def _pages_page(pages):
    class Page:
        def __init__(self):
            self.page_num = 0

        def content(self):
            return _state_html(pages[self.page_num])

    page = Page()
    urls = []

    def goto(_page, url):
        urls.append(url)
        page.page_num = len(urls) - 1

    return page, goto, urls


def test_paginated_topic_refs_empty_next_page_confirms_end(monkeypatch):
    """Пустая следующая страница — серверный контракт конца, не сдвиг окон:
    стоп по нулю новых без indeterminate."""

    page, goto, urls = _pages_page({0: [1, 2], 1: []})

    monkeypatch.setattr("hhru_bot.browser.goto_hh", goto)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _page: False)

    refs = paginated_topic_refs(page, max_pages=3)

    assert [ref.topic_id for ref in refs] == ["1", "2"]
    assert urls[-1] == "https://hh.ru/applicant/negotiations?page=1"


def test_paginated_topic_refs_shifted_window_raises_indeterminate(monkeypatch):
    """Ревью PR #1072: список сдвинулся между GET — окно page=1 целиком из уже
    виденных тем, но НЕ пересекается с началом прошлой выдачи (первой темы
    прошлой страницы в новой нет). Стоп по нулю новых молча отдал бы усечённый
    список за полный; вместо этого — ResponsesIndeterminate (fail-closed)."""

    # page 0: темы 1-5; между GET сверху встали новые темы → окно page=1
    # сползло назад на 3-5 (все уже seen), хвост 6+ остался за окном.
    page, goto, _urls = _pages_page({0: [1, 2, 3, 4, 5], 1: [3, 4, 5]})

    monkeypatch.setattr("hhru_bot.browser.goto_hh", goto)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _page: False)

    with pytest.raises(ResponsesIndeterminate, match="разошлись"):
        paginated_topic_refs(page, max_pages=3)


def test_combined_walk_shifted_window_raises_indeterminate(monkeypatch):
    """Тот же инвариант для комбинированного обхода (#710): сдвиг окна между
    страницами → ResponsesIndeterminate, а не молчаливая усечёнка тем/флагов
    напоминаний."""

    def remindable_state_html(topic_ids):
        state = {
            "applicantNegotiations": {
                "topicList": [
                    {
                        "id": tid,
                        "chatId": tid + 100,
                        "vacancyId": tid + 200,
                        "responseReminderState": {"allowed": False},
                    }
                    for tid in topic_ids
                ]
            }
        }
        return '<template id="HH-Lux-InitialState">' + json.dumps(state) + "</template>"

    pages = {0: [1, 2, 3, 4, 5], 1: [3, 4, 5]}

    class Page:
        def __init__(self):
            self.page_num = 0

        def content(self):
            return remindable_state_html(pages[self.page_num])

    page = Page()

    def goto(_page, url):
        page.page_num = 0 if url.endswith("negotiations") else int(url.rsplit("=", 1)[1])

    monkeypatch.setattr("hhru_bot.browser.goto_hh", goto)
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _page: True)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _page: False)

    with pytest.raises(ResponsesIndeterminate, match="разошлись"):
        paginated_topic_and_remindable_refs(page, max_pages=3)


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

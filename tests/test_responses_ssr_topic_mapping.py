"""Регрессия #186: SSR topic-recovery не должен захватывать элементы прошлой страницы.

``fetch_responses`` докидывает ``topic``/``chat_url`` из SSR ``topicList`` карточкам,
у которых их не было (open_chat — кнопка без href). Раньше это делалось срезом
``results[-count:]``, где ``count`` — число DOM-карточек ТЕКУЩЕЙ страницы; но
``parse_response_card`` пропускает карточки без vacancy_id (не добавляет их в
``results``), поэтому за одну итерацию в ``results`` могло добавиться МЕНЬШЕ
элементов, чем ``count``. Тогда срез ``-count:`` реально захватывал "хвост" из
результатов ПРЕДЫДУЩЕЙ страницы и мог присвоить им topic из SSR-состояния текущей
страницы — порча identity уже разобранной переписки. Фикс — считать точную длину
добавленного за страницу среза (``page_start = len(results)`` до цикла парсинга,
слайс ``results[page_start:]`` после), не полагаясь на ``count``.
"""

from __future__ import annotations

import pytest

import hhru_bot.responses as responses
from hhru_bot.browser import LOGIN_FORM
from hhru_bot.selector_groups import negotiations as ns

pytestmark = pytest.mark.integration


class _CardsLocator:
    def __init__(self, cards: list[object]):
        self.cards = cards

    def count(self):
        return len(self.cards)

    @property
    def first(self):
        return self

    def wait_for(self, *, state: str, timeout: int):  # noqa: ARG002
        return None

    def nth(self, index: int):
        return self.cards[index]


class _SSRPage:
    """Playwright Page fake с ``content()`` (нужен fetch_responses'у для SSR-mapping)."""

    def __init__(self, pages_cards: list[list[object]], pages_html: list[str]):
        self.url = "https://hh.ru/applicant/negotiations"
        self._pages_cards = pages_cards
        self._pages_html = pages_html
        self._page_num = -1

    def goto_page(self, page_num: int) -> None:
        self._page_num = page_num

    def locator(self, selector: str):
        if selector == LOGIN_FORM:
            return _CardsLocator([])
        assert selector == ns.NEGOTIATION_ITEM
        return _CardsLocator(self._pages_cards[self._page_num])

    def content(self) -> str:
        return self._pages_html[self._page_num]


def _ssr_html(topics: list[tuple[str, str, str]]) -> str:
    """``topics``: список (topic_id, chat_id, vacancy_id) → embedded SSR JSON."""
    entries = ",".join(f'{{"id":{t},"chatId":{c},"vacancyId":{v}}}' for t, c, v in topics)
    return f'<template id="HH-Lux-InitialState">{{"applicantNegotiations":{{"topicList":[{entries}]}}}}</template>'


def test_ssr_topic_recovery_propagates_resume_id(monkeypatch):
    item = responses.ResponseItem(vacancy_id="200", status=responses.ResponseStatus.READ)
    html = (
        '<template id="HH-Lux-InitialState">'
        '{"applicantNegotiations":{"topicList":['
        '{"id":999,"chatId":888,"vacancyId":200,"resumeId":321}'
        "]}}</template>"
    )
    page = _SSRPage([[item]], [html])
    monkeypatch.setattr(responses, "goto_hh", lambda page, _url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *_: False)

    result = responses.fetch_responses(page, max_pages=1)[0]

    assert (result.vacancy_id, result.topic, result.resume_id) == ("200", "999", "321")


def test_ssr_mapping_enriches_resume_when_dom_already_has_topic(monkeypatch):
    item = responses.ResponseItem(
        vacancy_id="200", status=responses.ResponseStatus.READ, topic="999"
    )
    html = (
        '<template id="HH-Lux-InitialState">'
        '{"applicantNegotiations":{"topicList":['
        '{"id":999,"chatId":888,"vacancyId":200,"resumeId":321}'
        "]}}</template>"
    )
    page = _SSRPage([[item]], [html])
    monkeypatch.setattr(responses, "goto_hh", lambda page, _url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *_: False)

    result = responses.fetch_responses(page, max_pages=1)[0]

    assert result.resume_id == "321"


def test_ssr_topic_recovery_does_not_leak_into_previous_page_when_page_skips_a_card(
    monkeypatch,
):
    """Страница 0: 1 DOM-карточка → item(vacancy=100), topic=None (SSR страницы 0
    ничего для vacancy=100 не даёт — как если бы open_chat ещё не был кликабелен).
    Страница 1: 2 DOM-карточки, но одна не парсится (vacancy_id отсутствует) →
    добавляется только item(vacancy=200); DOM count() страницы 1 равен 2. SSR-
    состояние страницы 1 (единственное, что реально видит parse на этой итерации)
    содержит topic'и для ОБОИХ vacancy=100 и vacancy=200 — это то, что позволяет
    отличить баг от фикса.

    Баг (``results[-count:]`` с ``count=2``) захватил бы ОБА результата — item(100)
    с прошлой страницы и item(200) — и присвоил бы item(100) topic из SSR-состояния
    страницы 1, хотя реально на этой странице карточки vacancy=100 не было. Фикс
    (срез по ``page_start``) должен трогать только item(200); item(100) обязан
    остаться как есть (topic=None), потому что его карточка была разобрана на
    предыдущей итерации, до того как появилось это SSR-состояние.
    """
    goto_calls: list[str] = []

    def goto(page, url):
        goto_calls.append(url)
        page.goto_page(len(goto_calls) - 1)

    def parse_card(card):
        return card  # cards are already ResponseItem-like stand-ins

    page0_item = responses.ResponseItem(vacancy_id="100", status=responses.ResponseStatus.READ)
    page1_item = responses.ResponseItem(vacancy_id="200", status=responses.ResponseStatus.READ)

    page = _SSRPage(
        pages_cards=[
            [page0_item],
            [None, page1_item],  # first card fails to parse (vacancy_id missing)
        ],
        pages_html=[
            _ssr_html([]),  # page 0's own SSR state has nothing for vacancy 100
            # page 1's SSR state offers topics for BOTH vacancy 100 and 200 —
            # only reachable by a card actually rendered on this page load.
            _ssr_html([("111", "777", "100"), ("999", "888", "200")]),
        ],
    )

    monkeypatch.setattr(responses, "goto_hh", goto)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", parse_card)
    monkeypatch.setattr(responses, "_has_next_page", lambda _page, page_num: page_num == 0)

    results = responses.fetch_responses(page, max_pages=2)

    assert [r.vacancy_id for r in results] == ["100", "200"]
    # Page 0's item must stay untouched by page 1's SSR state, even though
    # page 1's SSR state has an entry for the same vacancy_id.
    assert results[0].topic is None
    assert results[0].chat_url is None
    # Page 1's item is the only one eligible for this page's SSR topic.
    assert results[1].topic == "999"
    assert results[1].chat_url == "https://chatik.hh.ru/chat/888"


def test_ssr_topic_recovery_fails_closed_when_multiple_topics_share_one_vacancy(
    monkeypatch, caplog
):
    """Регрессия #186 (round 2): один работодатель с несколькими переписками по
    одной вакансии (несколько topic на один vacancy_id) раньше сопоставлялся
    позиционно (FIFO ``candidates.pop(0)``) — недоказанное допущение, что DOM-
    порядок карточек совпадает с порядком SSR ``topicList``. Если бы порядок
    расходился, чужой topic/chat_url присвоился бы не той карточке, испортив
    identity переписки (`(vacancy_id, topic)` — ключ истории).

    Фикс: при >1 кандидате на vacancy_id — НЕ гадать, оставить обе карточки без
    topic и залогировать warning. Единственный кандидат по-прежнему сопоставляется
    (это однозначно — см. соседний тест на странице 1 с одним vacancy=200 topic).
    """
    goto_calls: list[str] = []

    def goto(page, url):
        goto_calls.append(url)
        page.goto_page(len(goto_calls) - 1)

    def parse_card(card):
        return card

    item_a = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    item_b = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)

    page = _SSRPage(
        pages_cards=[[item_a, item_b]],
        pages_html=[
            # Two negotiations for the SAME vacancy_id=700 — reversed order vs
            # DOM (topic 222 listed first in SSR, but nothing pins it to item_a
            # specifically) is exactly the ambiguity the guard must reject.
            _ssr_html([("222", "333", "700"), ("444", "555", "700")]),
        ],
    )

    monkeypatch.setattr(responses, "goto_hh", goto)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", parse_card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with caplog.at_level("WARNING", logger="hhru_bot.responses"):
        results = responses.fetch_responses(page, max_pages=1)

    assert len(results) == 2
    # Neither card was guessed at — both stay unresolved rather than risking a
    # cross-assignment between two distinct negotiations for the same employer.
    assert results[0].topic is None
    assert results[0].chat_url is None
    assert results[1].topic is None
    assert results[1].chat_url is None
    assert any("неоднозначно" in message for message in caplog.messages)
    # Regression #186 (round 3): topic=None alone doesn't distinguish "no chat"
    # (e.g. discard) from "ambiguous SSR mapping" — callers persisting to
    # history.upsert_response (keyed on vacancy_id + topic IS NULL) need this
    # flag to avoid merging distinct negotiations into one history row.
    assert results[0].topic_ambiguous is True
    assert results[1].topic_ambiguous is True


def test_ssr_topic_recovery_leaves_topic_ambiguous_false_for_genuinely_chatless_card(
    monkeypatch,
):
    """Контроль: карточка без чата вовсе (нет SSR-кандидатов для её vacancy_id,
    напр. discard) должна остаться topic_ambiguous=False — иначе commands/responses
    пропускал бы её персистенцию без причины.
    """
    goto_calls: list[str] = []

    def goto(page, url):
        goto_calls.append(url)
        page.goto_page(len(goto_calls) - 1)

    def parse_card(card):
        return card

    item = responses.ResponseItem(vacancy_id="900", status=responses.ResponseStatus.DISCARD)

    page = _SSRPage(
        pages_cards=[[item]],
        pages_html=[_ssr_html([])],  # no SSR topics at all for this vacancy
    )

    monkeypatch.setattr(responses, "goto_hh", goto)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", parse_card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    results = responses.fetch_responses(page, max_pages=1)

    assert len(results) == 1
    assert results[0].topic is None
    assert results[0].topic_ambiguous is False


def test_ssr_topic_recovery_flags_all_cards_when_candidate_pool_is_smaller_than_card_count(
    monkeypatch, caplog
):
    """Регрессия #186 (round 4): две карточки одной вакансии, но SSR-состояние
    содержит только ОДИН topic-кандидат (напр. неполный/устаревший SSR state).
    Раньше первая карточка отбирала единственного кандидата через
    ``candidates.pop(0)`` (ветка ``len==1``), опустошая общий список в
    ``refs_by_vacancy`` — а вторая карточка того же vacancy_id видела уже пустой
    список (``len==0``), не попадала ни в "единственный кандидат", ни в "больше
    одного кандидата" ветку, и молча оставалась ``topic_ambiguous=False`` —
    как будто у неё легитимно нет чата, хотя на самом деле сопоставление
    неоднозначно ровно так же, как в случае с избытком кандидатов.

    Фикс группирует карточки по vacancy_id ДО принятия решения (без мутации
    общего списка кандидатов итеративным pop) — при любом несовпадении числа
    карточек и кандидатов (не только "кандидатов больше") ВСЕ карточки этой
    вакансии помечаются ambiguous.
    """
    goto_calls: list[str] = []

    def goto(page, url):
        goto_calls.append(url)
        page.goto_page(len(goto_calls) - 1)

    def parse_card(card):
        return card

    item_a = responses.ResponseItem(vacancy_id="800", status=responses.ResponseStatus.READ)
    item_b = responses.ResponseItem(vacancy_id="800", status=responses.ResponseStatus.READ)

    page = _SSRPage(
        pages_cards=[[item_a, item_b]],
        pages_html=[
            # Only ONE SSR topic for vacancy_id=800, but TWO cards need pairing.
            _ssr_html([("111", "222", "800")]),
        ],
    )

    monkeypatch.setattr(responses, "goto_hh", goto)
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", parse_card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with caplog.at_level("WARNING", logger="hhru_bot.responses"):
        results = responses.fetch_responses(page, max_pages=1)

    assert len(results) == 2
    # Neither card was guessed at — the mismatch (2 cards, 1 candidate) means
    # BOTH stay unresolved, not just the second one that would have drained
    # the shared candidate list under the old pop()-based logic.
    assert results[0].topic is None
    assert results[0].chat_url is None
    assert results[0].topic_ambiguous is True
    assert results[1].topic is None
    assert results[1].chat_url is None
    assert results[1].topic_ambiguous is True
    assert any("неоднозначно" in message for message in caplog.messages)


def test_strict_sync_rejects_unmatched_ssr_topic(monkeypatch):
    """Applied sync must not silently omit an SSR negotiation absent from DOM."""
    item = responses.ResponseItem(
        vacancy_id="700", status=responses.ResponseStatus.READ, topic="222", resume_id="r1"
    )
    page = _SSRPage(
        pages_cards=[[item]],
        pages_html=[_ssr_html([("222", "333", "700"), ("444", "555", "701")])],
    )
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="полного однозначного"):
        responses.fetch_responses(page, max_pages=1, strict_empty=True)


def test_strict_scrape_rejects_partial_dom_against_ssr_topics(monkeypatch):
    """Alert polling must not checkpoint while only part of the page is attached."""
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    page = _SSRPage(
        pages_cards=[[item]],
        pages_html=[_ssr_html([("222", "333", "700"), ("444", "555", "701")])],
    )
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="не покрывает SSR topicList"):
        responses.fetch_responses(page, max_pages=1, strict_scrape=True)


def test_strict_scrape_matches_ssr_topics_to_dom_vacancies(monkeypatch):
    """Equal DOM/SSR counts do not hide a missing vacancy card."""
    items = [
        responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ),
        responses.ResponseItem(vacancy_id="900", status=responses.ResponseStatus.DISCARD),
    ]
    page = _SSRPage(
        pages_cards=[items],
        pages_html=[_ssr_html([("222", "333", "700"), ("444", "555", "701")])],
    )
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="не покрывает SSR topicList"):
        responses.fetch_responses(page, max_pages=1, strict_scrape=True)


def test_strict_scrape_rejects_two_ssr_topics_for_one_rendered_card(monkeypatch):
    """Two SSR negotiations for one vacancy, only one rendered card, is indeterminate.

    Codex review round 2 of PR #768 claimed this shape ("more SSR candidates
    than rendered cards for one vacancy") slips past the coverage check and
    reaches the ambiguity-resolution branch as a silently accepted
    topic_ambiguous=True card. It does not: rendered(1) < candidates(2) makes
    missing_vacancies non-empty, so the coverage check above raises
    ResponsesIndeterminate before ambiguity resolution ever runs. This test
    pins that behavior (verified the Codex claim was a false positive).
    """
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    page = _SSRPage(
        pages_cards=[[item]],
        pages_html=[_ssr_html([("222", "333", "700"), ("444", "555", "700")])],
    )
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="не покрывает SSR topicList"):
        responses.fetch_responses(page, max_pages=1, strict_scrape=True)


def test_strict_sync_rejects_unattributed_dom_card(monkeypatch):
    """A rendered card without SSR resume/topic identity is not importable."""
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    page = _SSRPage(pages_cards=[[item]], pages_html=[_ssr_html([("222", "333", "700")])])
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="полного однозначного"):
        responses.fetch_responses(page, max_pages=1, strict_empty=True)


# #742: strict_scrape (--alert-new) must not silently accept a partially
# attached DOM as evidence of a confirmed-empty SSR topicList. Covers the
# acceptance criteria in issue #742: distinguish a confirmed empty SSR topic
# list from missing/malformed SSR state, treat dropped/incomplete topic_refs()
# entries as indeterminate rather than empty, preserve the chatless-card
# allowance, and never advance on uncertain page state.


def test_strict_scrape_accepts_confirmed_empty_ssr_topic_list(monkeypatch):
    """A genuinely empty SSR topicList (parsed successfully) is legitimate.

    No chat-having cards exist to cover, so the coverage check must not
    treat "SSR unavailable" and "SSR confirms zero negotiations" the same.
    """
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    page = _SSRPage(pages_cards=[[item]], pages_html=[_ssr_html([])])
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    results = responses.fetch_responses(page, max_pages=1, strict_scrape=True)

    assert len(results) == 1
    assert results[0].topic_ambiguous is False


def test_strict_scrape_rejects_missing_ssr_state(monkeypatch):
    """Missing HH-Lux-InitialState must not fail open under strict_scrape."""
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    page = _SSRPage(pages_cards=[[item]], pages_html=["<html><body>no SSR template</body></html>"])
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="SSR topic/resume mapping"):
        responses.fetch_responses(page, max_pages=1, strict_scrape=True)


def test_strict_scrape_rejects_malformed_ssr_json(monkeypatch):
    """Malformed JSON inside the SSR template must not fail open either."""
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    broken_html = '<template id="HH-Lux-InitialState">{not valid json</template>'
    page = _SSRPage(pages_cards=[[item]], pages_html=[broken_html])
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="SSR topic/resume mapping"):
        responses.fetch_responses(page, max_pages=1, strict_scrape=True)


def test_strict_scrape_rejects_incomplete_topic_dropped_by_topic_refs(monkeypatch):
    """topic_refs() silently drops entries missing id/chatId/vacancyId.

    If that drop empties `refs` while the raw SSR topicList is non-empty,
    strict_scrape must treat it as indeterminate coverage, not as a
    confirmed-empty list (the #742 regression: `strict_scrape and refs`
    used to skip the coverage check entirely in this case).
    """
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    # A single topic entry missing chatId -> topic_refs() drops it -> refs == [].
    incomplete_html = (
        '<template id="HH-Lux-InitialState">'
        '{"applicantNegotiations":{"topicList":[{"id":222,"vacancyId":700}]}}'
        "</template>"
    )
    page = _SSRPage(pages_cards=[[item]], pages_html=[incomplete_html])
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="неполную запись"):
        responses.fetch_responses(page, max_pages=1, strict_scrape=True)


# #742 round 2 (Codex adversarial review of PR #768): topic_refs() is called
# BEFORE the raw_topics shape validation, and its .get() calls raise
# AttributeError on a null applicantNegotiations or a non-dict topicList
# entry — a shape neither the original except-tuple nor the new
# raw_topics-based checks could catch, since the crash happens one line
# earlier. Without AttributeError in the except-tuple, this shape escaped as
# an unhandled traceback instead of ResponsesIndeterminate — fail-open
# despite the #742 fix.


def test_strict_scrape_rejects_null_applicant_negotiations(monkeypatch):
    """applicantNegotiations: null must not crash topic_refs() uncaught."""
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    null_html = '<template id="HH-Lux-InitialState">{"applicantNegotiations":null}</template>'
    page = _SSRPage(pages_cards=[[item]], pages_html=[null_html])
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="SSR topic/resume mapping"):
        responses.fetch_responses(page, max_pages=1, strict_scrape=True)


def test_strict_scrape_rejects_non_dict_topic_entry(monkeypatch):
    """A non-dict entry in topicList must not crash topic_refs() uncaught."""
    item = responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ)
    non_dict_html = (
        '<template id="HH-Lux-InitialState">'
        '{"applicantNegotiations":{"topicList":[123]}}'
        "</template>"
    )
    page = _SSRPage(pages_cards=[[item]], pages_html=[non_dict_html])
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    with pytest.raises(responses.ResponsesIndeterminate, match="SSR topic/resume mapping"):
        responses.fetch_responses(page, max_pages=1, strict_scrape=True)


def test_strict_scrape_preserves_chatless_card_allowance(monkeypatch):
    """A card genuinely without a chat is fine alongside a fully covered one.

    strict_scrape must reject only unconfirmed page state, not a vacancy
    that legitimately has no negotiation chat attached — vacancy_id=900 has
    no SSR entries at all (nothing to miss), while vacancy_id=700 has an
    exact 1:1 DOM/SSR match.
    """
    items = [
        responses.ResponseItem(vacancy_id="700", status=responses.ResponseStatus.READ),
        responses.ResponseItem(vacancy_id="900", status=responses.ResponseStatus.DISCARD),
    ]
    page = _SSRPage(
        pages_cards=[items],
        pages_html=[_ssr_html([("222", "333", "700")])],
    )
    monkeypatch.setattr(responses, "goto_hh", lambda page, url: page.goto_page(0))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda page: True)
    monkeypatch.setattr(responses, "parse_response_card", lambda card: card)
    monkeypatch.setattr(responses, "_has_next_page", lambda *args: False)

    results = responses.fetch_responses(page, max_pages=1, strict_scrape=True)

    assert len(results) == 2
    chatless = next(r for r in results if r.vacancy_id == "900")
    assert chatless.topic is None
    assert chatless.topic_ambiguous is False

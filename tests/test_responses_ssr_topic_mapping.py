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

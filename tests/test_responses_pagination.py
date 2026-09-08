"""Регрессии #141: pagination negotiations не должна ложно завершать обход."""

from __future__ import annotations

import pytest

import hhru_bot.responses as responses
from hhru_bot.browser import LOGIN_FORM
from hhru_bot.selector_groups import negotiations as ns

pytestmark = pytest.mark.integration


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
        self.cards = _DelayedCardsLocator(cards, delayed_cards)

    def locator(self, selector: str):
        if selector == LOGIN_FORM:
            return _Locator([])
        assert selector == ns.NEGOTIATION_ITEM
        return self.cards


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


def test_fetch_responses_rejects_nonpositive_page_limit():
    with pytest.raises(ValueError, match="positive"):
        responses.fetch_responses(object(), max_pages=0)


# --- pagerless (#1067): конец списка без пейджера — по нулю новых топиков ---


class _PagerlessPage:
    """Фейк Page для pagerless-обхода: URL -> (карточки, SSR html)."""

    BASE = "https://hh.ru/applicant/negotiations"

    def __init__(self, pages: dict[str, tuple[list, str]]):
        self._pages = pages
        self._cards: list = []
        self._html = ""
        self.visited: list[str] = []

    def goto(self, url: str, *, wait_until: str = ""):  # noqa: ARG002
        self.visited.append(url)
        self._cards, self._html = self._pages[url]

    @property
    def context(self):
        class _Ctx:
            def cookies(self):
                return [{"name": "hhtoken", "value": "x"}]

        return _Ctx()

    def locator(self, selector: str):
        if selector == LOGIN_FORM:
            return _Locator([])
        assert selector == ns.NEGOTIATION_ITEM
        return _PagerlessCardsLocator(self._cards)

    def content(self) -> str:
        return self._html


class _PagerlessCardsLocator:
    """Карточки уже «отрендерены» фейком; wait_for ничего не ждёт."""

    def __init__(self, cards: list):
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


def _ssr(topics: list[tuple[int, int, str]]) -> str:
    entries = ",".join(f'{{"id":{t},"chatId":{c},"vacancyId":"{v}"}}' for t, c, v in topics)
    return (
        '<template id="HH-Lux-InitialState">'
        f'{{"applicantNegotiations":{{"topicList":[{entries}]}}}}'
        "</template>"
    )


def _run_pagerless(monkeypatch, page):
    monkeypatch.setattr(responses, "goto_hh", lambda p, url: p.goto(url))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(
        responses,
        "parse_response_card",
        lambda card: responses.ResponseItem(vacancy_id=card, status=responses.ResponseStatus.READ),
    )
    return page


def test_pagerless_reads_lazy_tail_beyond_missing_pager(monkeypatch):
    """Дрейф 2026-09-09 (#1067): пейджера нет, но хвост за первой двадцаткой жив.

    Страница 1 приносит НОВЫЕ топики — обход обязан их собрать: раньше
    «пейджер не отрисован» читался как «страница одна» и clear-negotiations
    видел только первые 20 тем.
    """
    url0, url1 = _PagerlessPage.BASE, f"{_PagerlessPage.BASE}?page=1"
    page = _PagerlessPage(
        {
            url0: (["1", "2"], _ssr([(1, 11, "1"), (2, 22, "2")])),
            url1: (["3", "4"], _ssr([(3, 33, "3"), (4, 44, "4")])),
            f"{_PagerlessPage.BASE}?page=2": ([], _ssr([])),
        }
    )
    _run_pagerless(monkeypatch, page)

    items = responses.fetch_responses(page, max_pages=3, pagerless=True)

    assert sorted(i.vacancy_id for i in items) == ["1", "2", "3", "4"]
    assert page.visited == [url0, url1, f"{_PagerlessPage.BASE}?page=2"]


def test_pagerless_stops_when_server_ignores_page_param(monkeypatch):
    """Сервер, игнорирующий ?page, возвращает те же темы: ноль новых -> стоп
    без дублей (карточки не задваиваются)."""
    url0 = _PagerlessPage.BASE
    same = (["1"], _ssr([(1, 11, "1")]))
    page = _PagerlessPage({url0: same, f"{url0}?page=1": same, f"{url0}?page=2": same})
    _run_pagerless(monkeypatch, page)

    items = responses.fetch_responses(page, max_pages=3, pagerless=True)

    # Карточки повторной страницы попадают в results (дедуп делает upsert в
    # истории по UNIQUE) — но обход обязан остановиться, не дойдя до page=2.
    assert [i.vacancy_id for i in items] == ["1", "1"]
    assert page.visited == [url0, f"{url0}?page=1"]


def test_pagerless_cap_with_nonempty_page_is_indeterminate(monkeypatch):
    """Потолок --max-pages при непустой последней странице: полнота списка не
    подтверждена — ResponsesIndeterminate (clear-negotiations превращает его
    в отказ ДО отзыва, инвариант PR #196)."""
    url0 = _PagerlessPage.BASE
    page = _PagerlessPage({url0: (["1"], _ssr([(1, 11, "1")]))})
    _run_pagerless(monkeypatch, page)

    with pytest.raises(responses.ResponsesIndeterminate, match="--max-pages"):
        responses.fetch_responses(page, max_pages=1, pagerless=True)


def test_pagerless_unreadable_ssr_is_indeterminate(monkeypatch):
    """Ноль новых — единственное доказательство конца; нечитаемый SSR не должен
    превращаться в «дочитано» (fail-closed)."""
    url0 = _PagerlessPage.BASE
    page = _PagerlessPage({url0: (["1"], "<html>no ssr</html>")})
    _run_pagerless(monkeypatch, page)

    with pytest.raises(responses.ResponsesIndeterminate, match="SSR topicList"):
        responses.fetch_responses(page, max_pages=2, pagerless=True)

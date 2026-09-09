"""Пагинация negotiations — pagerless-семантика (#1074, наследие #141/#1067).

Пейджер hh.ru удалил (дрейф 2026-09-09, PR #1066): конец списка доказывается
данными — страница без НОВЫХ SSR-топиков, а не отсутствующим UI-пейджером.
"""

from __future__ import annotations

import pytest

import hhru_bot.responses as responses
from hhru_bot.browser import LOGIN_FORM
from hhru_bot.selector_groups import negotiations as ns

pytestmark = pytest.mark.integration


class _EmptyLocator:
    def count(self):
        return 0

    @property
    def first(self):
        return self


class _PageSpec:
    """Одна страница фейкового списка: карточки + SSR html + режим рендера.

    render="rendered" — карточки уже в DOM; "delayed" — появляются после
    wait_for (bounded-render контракт); "timeout" — wait_for истекает (карточек
    нет и не будет за разумное время).
    """

    def __init__(self, cards, ssr, *, render="rendered", delayed_cards=None):
        self.cards = list(cards)
        self.delayed_cards = list(delayed_cards or [])
        self.ssr = ssr
        self.render = render


class _PagerlessPage:
    """Фейк Page для pagerless-обхода: URL -> _PageSpec."""

    BASE = "https://hh.ru/applicant/negotiations"

    def __init__(self, pages: dict[str, _PageSpec]):
        self._pages = pages
        self._spec: _PageSpec | None = None
        self.visited: list[str] = []

    def goto(self, url: str):
        self.visited.append(url)
        self._spec = self._pages[url]

    @property
    def context(self):
        class _Ctx:
            def cookies(self):
                return [{"name": "hhtoken", "value": "x"}]

        return _Ctx()

    def locator(self, selector: str):
        if selector == LOGIN_FORM:
            return _EmptyLocator()
        assert selector == ns.NEGOTIATION_ITEM
        return _PagerlessCardsLocator(self._spec)

    def content(self) -> str:
        return self._spec.ssr


class _PagerlessCardsLocator:
    def __init__(self, spec: _PageSpec):
        self.spec = spec

    def count(self):
        return len(self.spec.cards)

    @property
    def first(self):
        return self

    def wait_for(self, *, state: str, timeout: int):  # noqa: ARG002
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if self.spec.render == "delayed":
            self.spec.cards.extend(self.spec.delayed_cards)
            return
        if self.spec.render == "timeout":
            raise PlaywrightTimeoutError("response cards did not render")
        return None

    def nth(self, index: int):
        return self.spec.cards[index]


def _ssr(topics: list[tuple[int, int, str]]) -> str:
    entries = ",".join(f'{{"id":{t},"chatId":{c},"vacancyId":"{v}"}}' for t, c, v in topics)
    return (
        '<template id="HH-Lux-InitialState">'
        f'{{"applicantNegotiations":{{"topicList":[{entries}]}}}}'
        "</template>"
    )


def _item(card: str) -> responses.ResponseItem:
    return responses.ResponseItem(vacancy_id=card, status=responses.ResponseStatus.READ)


def _run(monkeypatch, page):
    monkeypatch.setattr(responses, "goto_hh", lambda p, url: p.goto(url))
    monkeypatch.setattr(responses, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(responses, "parse_response_card", _item)
    return page


def test_fetch_responses_waits_for_delayed_cards(monkeypatch):
    url0, url1 = _PagerlessPage.BASE, f"{_PagerlessPage.BASE}?page=1"
    page = _PagerlessPage(
        {
            url0: _PageSpec([], _ssr([(1, 11, "1")]), render="delayed", delayed_cards=["1"]),
            url1: _PageSpec([], _ssr([])),
        }
    )
    _run(monkeypatch, page)

    items = responses.fetch_responses(page, max_pages=2)
    # SSR mapping обогащает карточку topic/chat_url — это ожидаемо.
    assert [i.vacancy_id for i in items] == ["1"]
    assert items[0].topic == "1"


def test_fetch_responses_timeout_preserves_empty_inbox_contract(monkeypatch):
    page = _PagerlessPage({_PagerlessPage.BASE: _PageSpec([], "", render="timeout")})
    _run(monkeypatch, page)

    assert responses.fetch_responses(page, max_pages=1) == []


def test_fetch_responses_timeout_on_confirmed_later_page_is_indeterminate(monkeypatch):
    """A confirmed non-empty first page must not let a later render timeout
    pass as an empty page: without SSR the end of the list is unprovable."""
    url0, url1 = _PagerlessPage.BASE, f"{_PagerlessPage.BASE}?page=1"
    page = _PagerlessPage(
        {
            url0: _PageSpec(["1"], _ssr([(1, 11, "1")])),
            url1: _PageSpec([], "", render="timeout"),
        }
    )
    _run(monkeypatch, page)

    with pytest.raises(responses.ResponsesIndeterminate, match="страницы 1"):
        responses.fetch_responses(page, max_pages=2)


def test_fetch_responses_dom_empty_later_page_with_empty_ssr_is_confirmed_end(monkeypatch):
    """Живой кейс #1074: конец ленты — страница без карточек И без empty-state-
    селектора. Подтверждение конца — пустой/повторный SSR topicList (семантика
    paginated_* обходчиков), а не DOM."""
    url0, url1 = _PagerlessPage.BASE, f"{_PagerlessPage.BASE}?page=1"
    page = _PagerlessPage(
        {
            url0: _PageSpec(["1"], _ssr([(1, 11, "1")])),
            url1: _PageSpec([], _ssr([]), render="timeout"),
        }
    )
    _run(monkeypatch, page)

    items = responses.fetch_responses(page, max_pages=2)

    assert [i.vacancy_id for i in items] == ["1"]
    assert page.visited == [url0, url1]


def test_fetch_responses_dom_empty_later_page_with_new_ssr_topics_is_indeterminate(
    monkeypatch,
):
    """SSR содержит НОВЫЕ топики, а карточки не отрисовались — данные не
    собраны, конец не подтверждён: indeterminate, а не тихая обрезка."""
    url0, url1 = _PagerlessPage.BASE, f"{_PagerlessPage.BASE}?page=1"
    page = _PagerlessPage(
        {
            url0: _PageSpec(["1"], _ssr([(1, 11, "1")])),
            url1: _PageSpec([], _ssr([(2, 22, "2")]), render="timeout"),
        }
    )
    _run(monkeypatch, page)

    with pytest.raises(responses.ResponsesIndeterminate, match="страницы 1"):
        responses.fetch_responses(page, max_pages=2)


def test_fetch_responses_rejects_nonpositive_page_limit():
    with pytest.raises(ValueError, match="positive"):
        responses.fetch_responses(object(), max_pages=0)


# --- pagerless (#1067/#1074): конец списка без пейджера — по нулю новых топиков ---


def test_pagerless_empty_feed_stops_on_first_page(monkeypatch):
    """Пустая лента: ноль карточек на странице 0 — обход завершается одним GET."""
    url0 = _PagerlessPage.BASE
    page = _PagerlessPage({url0: _PageSpec([], _ssr([]))})
    _run(monkeypatch, page)

    assert responses.fetch_responses(page, max_pages=3) == []
    assert page.visited == [url0]


def test_pagerless_single_page_feed_reads_confirming_second_get(monkeypatch):
    """Лента в одну страницу: page=1 возвращает пустую страницу — конец
    подтверждён данными, а не отсутствующим пейджером."""
    url0, url1 = _PagerlessPage.BASE, f"{_PagerlessPage.BASE}?page=1"
    page = _PagerlessPage(
        {
            url0: _PageSpec(["1", "2"], _ssr([(1, 11, "1"), (2, 22, "2")])),
            url1: _PageSpec([], _ssr([])),
        }
    )
    _run(monkeypatch, page)

    items = responses.fetch_responses(page, max_pages=3)

    assert [i.vacancy_id for i in items] == ["1", "2"]
    assert page.visited == [url0, url1]


def test_pagerless_reads_lazy_tail_beyond_missing_pager(monkeypatch):
    """Дрейф 2026-09-09 (#1067): пейджера нет, но хвост за первой двадцаткой жив.

    Страница 1 приносит НОВЫЕ топики — обход обязан их собрать: раньше
    «пейджер не отрисован» читался как «страница одна» и скан видел только
    первые ~20 тем.
    """
    url0, url1 = _PagerlessPage.BASE, f"{_PagerlessPage.BASE}?page=1"
    page = _PagerlessPage(
        {
            url0: _PageSpec(["1", "2"], _ssr([(1, 11, "1"), (2, 22, "2")])),
            url1: _PageSpec(["3", "4"], _ssr([(3, 33, "3"), (4, 44, "4")])),
            f"{_PagerlessPage.BASE}?page=2": _PageSpec([], _ssr([])),
        }
    )
    _run(monkeypatch, page)

    items = responses.fetch_responses(page, max_pages=3)

    assert sorted(i.vacancy_id for i in items) == ["1", "2", "3", "4"]
    assert page.visited == [url0, url1, f"{_PagerlessPage.BASE}?page=2"]


def test_pagerless_stops_when_server_ignores_page_param(monkeypatch):
    """Сервер, игнорирующий ?page, возвращает те же темы: ноль новых -> стоп
    без дублей (карточки не задваиваются)."""
    url0 = _PagerlessPage.BASE
    same = _PageSpec(["1"], _ssr([(1, 11, "1")]))
    page = _PagerlessPage({url0: same, f"{url0}?page=1": same, f"{url0}?page=2": same})
    _run(monkeypatch, page)

    items = responses.fetch_responses(page, max_pages=3)

    # Карточки повторной страницы попадают в results (дедуп делает upsert в
    # истории по UNIQUE) — но обход обязан остановиться, не дойдя до page=2.
    assert [i.vacancy_id for i in items] == ["1", "1"]
    assert page.visited == [url0, f"{url0}?page=1"]


def test_pagerless_list_shift_between_gets_keeps_new_topics(monkeypatch):
    """Сдвиг списка между GET (#1074): новый отклик в голове ленты сдвигает
    окна — перекрытие не должно ни потерять новый топик, ни зациклить обход."""
    url0 = _PagerlessPage.BASE
    page = _PagerlessPage(
        {
            url0: _PageSpec(["1", "2", "3"], _ssr([(1, 11, "1"), (2, 22, "2"), (3, 33, "3")])),
            # Между GET в голову вставился topic=0: окно page=1 вернуло
            # сдвинутый срез [0, 1, 2] — два дубля и ОДИН новый топик.
            f"{url0}?page=1": _PageSpec(
                ["0", "1", "2"], _ssr([(0, 10, "0"), (1, 11, "1"), (2, 22, "2")])
            ),
            # Дальше сдвиг прекратился: то же окно — ноль новых, конец.
            f"{url0}?page=2": _PageSpec(
                ["0", "1", "2"], _ssr([(0, 10, "0"), (1, 11, "1"), (2, 22, "2")])
            ),
        }
    )
    _run(monkeypatch, page)

    items = responses.fetch_responses(page, max_pages=4)

    assert sorted({i.vacancy_id for i in items}) == ["0", "1", "2", "3"]
    assert page.visited == [url0, f"{url0}?page=1", f"{url0}?page=2"]


def test_pagerless_cap_with_nonempty_page_is_indeterminate(monkeypatch):
    """Потолок --max-pages при непустой последней странице: полнота списка не
    подтверждена — ResponsesIndeterminate (тихая обрезка запрещена, #1074;
    clear-negotiations превращает это в отказ ДО отзыва, инвариант PR #196)."""
    url0 = _PagerlessPage.BASE
    page = _PagerlessPage({url0: _PageSpec(["1"], _ssr([(1, 11, "1")]))})
    _run(monkeypatch, page)

    with pytest.raises(responses.ResponsesIndeterminate, match="--max-pages"):
        responses.fetch_responses(page, max_pages=1)


def test_pagerless_unreadable_ssr_is_indeterminate(monkeypatch):
    """Ноль новых — единственное доказательство конца; нечитаемый SSR не должен
    превращаться в «дочитано» (fail-closed)."""
    url0 = _PagerlessPage.BASE
    page = _PagerlessPage({url0: _PageSpec(["1"], "<html>no ssr</html>")})
    _run(monkeypatch, page)

    with pytest.raises(responses.ResponsesIndeterminate, match="SSR topicList"):
        responses.fetch_responses(page, max_pages=2)

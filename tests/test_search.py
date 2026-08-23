"""Characterization-тесты чистой логики search.py.

Страхуют рефакторинг: build_search_url, _extract_vacancy_id, filter_candidates.
Поведение не должно измениться после реструктуризации.
"""

from __future__ import annotations

import pytest

import hhru_bot.search as search
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.search import (
    SalaryInfo,
    VacancyCard,
    _extract_vacancy_id,
    build_search_url,
    filter_candidates,
    rank_candidates,
)

pytestmark = pytest.mark.integration


class _DelayedCardsLocator:
    """count() видит DOM сейчас, .first.wait_for() — после JS-рендера."""

    def __init__(self, cards: list[object], delayed_cards: list[object] | None = None):
        self.cards = cards
        self.delayed_cards = delayed_cards
        self.wait_calls: list[tuple[str, int]] = []

    def count(self):
        return len(self.cards)

    @property
    def first(self):
        return self

    def wait_for(self, *, state: str, timeout: int):
        self.wait_calls.append((state, timeout))
        if self.cards:
            return
        if self.delayed_cards:
            self.cards.extend(self.delayed_cards)
            return
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        raise PlaywrightTimeoutError("vacancy cards did not render")

    def nth(self, index: int):
        return self.cards[index]


class _TextLocator:
    def __init__(self, text: str = "", href: str | None = None, count: int = 1):
        self.text = text
        self.href = href
        self._count = count

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def inner_text(self):
        return self.text

    def get_attribute(self, name: str):
        assert name == "href"
        return self.href


class _VacancyCard:
    def locator(self, selector: str):
        if selector == search.sel.VACANCY_CARD_TITLE_LINK:
            return _TextLocator("Python developer", "/vacancy/42")
        if selector == search.sel.VACANCY_CARD_COMPANY:
            return _TextLocator(count=0)
        # Доп. признаки карточки (#517) опциональны — как остальные блоки
        # этого мока, "не найдено" по умолчанию для любого нового селектора.
        if selector in (
            search.sel.VACANCY_CARD_REMOTE_LABEL,
            search.sel.VACANCY_CARD_EXPERIENCE,
        ):
            return _TextLocator(count=0)
        raise AssertionError(f"unexpected card selector: {selector}")

    def inner_text(self):
        return "Python developer"


class _SearchPage:
    def __init__(
        self,
        cards: list[object],
        delayed_cards: list[object] | None = None,
        empty: bool = False,
    ):
        self.cards_locator = _DelayedCardsLocator(cards, delayed_cards)
        self.empty_locator = _DelayedCardsLocator([object()] if empty else [])
        self.ready_selector: str | None = None

    def locator(self, selector: str):
        if selector == search.sel.VACANCY_CARD:
            return self.cards_locator
        if selector == search.sel.VACANCY_SEARCH_EMPTY:
            return self.empty_locator
        if selector == f"{search.sel.VACANCY_CARD_TITLE_LINK}, {search.sel.VACANCY_SEARCH_EMPTY}":
            self.ready_selector = selector
            return self.empty_locator if self.empty_locator.count() else self.cards_locator
        raise AssertionError(f"unexpected selector: {selector}")


def _search_filters():
    return SearchFilters(text="python")


def test_search_render_timeout_allows_current_hh_js_render_lag():
    """#455: 10 с недостаточно, хотя выдача появляется примерно за 20 с."""
    assert search.RENDER_TIMEOUT_MS == 30_000


def test_search_waits_for_delayed_cards_before_declaring_empty(monkeypatch):
    """Регрессия #141: JS-карточки могут появиться после goto_hh()."""
    page = _SearchPage([], delayed_cards=[_VacancyCard()])
    monkeypatch.setattr(search, "goto_hh", lambda *args, **kwargs: None)
    monkeypatch.setattr(search, "_has_next_page", lambda *args: False)
    monkeypatch.setattr(search, "_optional_text", lambda *args: None)
    monkeypatch.setattr(search, "_parse_employer_info", lambda *args: None)

    cards = search.search_vacancies(page, _search_filters(), max_pages=1)

    assert [card.vacancy_id for card in cards] == ["42"]
    assert page.cards_locator.wait_calls == [("attached", search.RENDER_TIMEOUT_MS)]
    assert page.ready_selector == (
        f"{search.sel.VACANCY_CARD_TITLE_LINK}, {search.sel.VACANCY_SEARCH_EMPTY}"
    )


def test_search_timeout_is_indeterminate_not_empty_result(monkeypatch):
    """Нулевой count без подтверждённого empty-state не должен обрывать обход."""
    page = _SearchPage([])
    monkeypatch.setattr(search, "goto_hh", lambda *args, **kwargs: None)

    monkeypatch.setattr(search.time, "sleep", lambda *_: None)
    with pytest.raises(search.VacancySearchIndeterminate, match="не подтвержден") as error:
        search.search_vacancies(page, _search_filters(), max_pages=1)
    assert error.value.state == "indeterminate"
    assert error.value.page_num == 0
    assert error.value.partial_results == []


def test_search_timeout_retries_current_page_once_then_returns_typed_partial(monkeypatch):
    page = _SearchPage([_VacancyCard()])
    calls = []

    def goto(_page, url):
        calls.append(url)
        if len(calls) >= 2:
            page.cards_locator.cards.clear()

    monkeypatch.setattr(search, "goto_hh", goto)
    monkeypatch.setattr(search.time, "sleep", lambda *_: None)
    monkeypatch.setattr(search, "_has_next_page", lambda _page, page_num: page_num == 0)
    monkeypatch.setattr(search, "_optional_text", lambda *args: None)
    monkeypatch.setattr(search, "_parse_employer_info", lambda *args: None)

    with pytest.raises(search.VacancySearchIndeterminate) as error:
        search.search_vacancies(page, _search_filters(), max_pages=2)

    assert len(calls) == 3  # page 0, page 1, one retry of page 1
    assert error.value.page_num == 1
    assert [card.vacancy_id for card in error.value.partial_results] == ["42"]
    assert error.value.diagnostics["card_count"] == 0


def test_search_returns_empty_only_after_confirmed_empty_state(monkeypatch):
    page = _SearchPage([], empty=True)
    monkeypatch.setattr(search, "goto_hh", lambda *args, **kwargs: None)

    assert search.search_vacancies(page, _search_filters(), max_pages=1) == []


def test_search_navigation_failure_is_typed_unreachable(monkeypatch):
    page = _SearchPage([])
    monkeypatch.setattr(search.time, "sleep", lambda *_: None)

    def unreachable(*_args, **_kwargs):
        from playwright.sync_api import Error as PlaywrightError

        raise PlaywrightError("network down")

    monkeypatch.setattr(search, "goto_hh", unreachable)
    with pytest.raises(search.VacancySearchIndeterminate) as error:
        search.search_vacancies(page, _search_filters(), max_pages=1)
    assert error.value.state == "unreachable"


def test_search_login_page_is_typed_unauthenticated(monkeypatch):
    page = _SearchPage([])
    page.url = "https://hh.ru/account/login"
    monkeypatch.setattr(search, "goto_hh", lambda *_: None)
    monkeypatch.setattr(search.time, "sleep", lambda *_: None)

    with pytest.raises(search.VacancySearchIndeterminate) as error:
        search.search_vacancies(page, _search_filters(), max_pages=1)
    assert error.value.state == "unauthenticated"


class FakeHistory:
    """История, которая знает только заданный набор (resume_id, vacancy_id)."""

    def __init__(
        self,
        applied: set[tuple[str, str]] | None = None,
        skipped: set[tuple[str, str]] | None = None,
    ):
        self._applied = applied or set()
        self._skipped = skipped or set()
        # Журнал записанных skip-причин для проверки интеграции (#87).
        self.recorded_skips: list[tuple[str, str, str]] = []

    def has_applied(self, resume_id: str, vacancy_id: str) -> bool:
        return (resume_id, vacancy_id) in self._applied

    def is_skipped(self, resume_id: str, vacancy_id: str) -> bool:
        return (resume_id, vacancy_id) in self._skipped

    def is_skipped_for(self, resume_id: str, vacancy_id: str, reason: str) -> bool:  # noqa: ARG002
        return (resume_id, vacancy_id) in self._skipped

    def record_skip(self, resume_id: str, vacancy_id: str, reason: str) -> None:
        self.recorded_skips.append((resume_id, vacancy_id, reason))


def card(
    vacancy_id: str, title: str = "T", company: str = "C", url: str = "https://hh.ru/vacancy/0"
):
    return VacancyCard(vacancy_id=vacancy_id, title=title, company=company, url=url)


# --- build_search_url ---


def test_build_search_url_minimal():
    url = build_search_url(SearchFilters(text="python"))
    assert url.startswith("https://hh.ru/search/vacancy?")
    assert "text=python" in url
    assert "page=0" in url


def test_build_search_url_all_filters():
    url = build_search_url(
        SearchFilters(
            text="data analyst",
            area=1,
            salary_from=200000,
            experience="between3And6",
            schedule="remote",
        ),
        page_num=2,
    )
    assert "text=data+analyst" in url
    assert "page=2" in url
    assert "area=1" in url
    assert "salary=200000" in url
    assert "experience=between3And6" in url
    assert "schedule=remote" in url


# --- _extract_vacancy_id ---


def test_extract_vacancy_id_plain_url():
    assert _extract_vacancy_id("https://hh.ru/vacancy/123456") == "123456"


def test_extract_vacancy_id_with_query():
    assert _extract_vacancy_id("/vacancy/98765?from=serp") == "98765"


def test_extract_vacancy_id_non_numeric():
    assert _extract_vacancy_id("https://hh.ru/vacancy/abc") is None


def test_extract_vacancy_id_empty():
    assert _extract_vacancy_id("") is None


# --- filter_candidates ---


def test_filter_candidates_keeps_clean_cards():
    filters = SearchFilters(text="x")
    cards = [card("1"), card("2")]
    candidates, skipped = filter_candidates(cards, filters, "r1", FakeHistory())
    assert candidates == cards
    assert skipped == []


def test_filter_candidates_drops_already_applied():
    filters = SearchFilters(text="x")
    cards = [card("1"), card("2")]
    history = FakeHistory(applied={("r1", "1")})
    candidates, skipped = filter_candidates(cards, filters, "r1", history)
    assert [c.vacancy_id for c in candidates] == ["2"]
    assert len(skipped) == 1
    assert skipped[0][0].vacancy_id == "1"
    assert "уже откликались" in skipped[0][1]


def test_filter_candidates_excludes_employers():
    filters = SearchFilters(text="x", exclude_employers=["BadCorp"])
    cards = [
        card("1", title="Dev", company="GoodCorp"),
        card("2", title="Dev", company="BadCorp Inc"),
    ]
    candidates, skipped = filter_candidates(cards, filters, "r1", FakeHistory())
    assert [c.vacancy_id for c in candidates] == ["1"]
    assert skipped[0][0].vacancy_id == "2"
    assert "стоп-списке" in skipped[0][1]


def test_filter_candidates_excludes_keywords():
    filters = SearchFilters(text="x", exclude_keywords=["1С"])
    cards = [
        card("1", title="Python Dev"),
        card("2", title="Программист 1С"),
    ]
    candidates, skipped = filter_candidates(cards, filters, "r1", FakeHistory())
    assert [c.vacancy_id for c in candidates] == ["1"]
    assert skipped[0][0].vacancy_id == "2"
    assert "стоп-слово" in skipped[0][1]


# --- filter_candidates: запись skip-причин в журнал skipped (#87) -----------


def test_filter_candidates_records_skip_reason_for_already_applied():
    """#87: отсеянная «уже откликались» вакансия пишется в журнал skipped."""
    from hhru_bot.history import SKIP_REASONS

    filters = SearchFilters(text="x")
    history = FakeHistory(applied={("r1", "1")})
    filter_candidates([card("1"), card("2")], filters, "r1", history)
    assert ("r1", "1", SKIP_REASONS.ALREADY_APPLIED) in history.recorded_skips


def test_filter_candidates_records_skip_reason_for_excluded_employer():
    """#87: стоп-компания → reason STOPWORD_EMPLOYER в журнале."""
    from hhru_bot.history import SKIP_REASONS

    filters = SearchFilters(text="x", exclude_employers=["BadCorp"])
    history = FakeHistory()
    filter_candidates([card("1", title="Dev", company="BadCorp Inc")], filters, "r1", history)
    assert ("r1", "1", SKIP_REASONS.STOPWORD_EMPLOYER) in history.recorded_skips


def test_filter_candidates_records_skip_reason_for_excluded_keyword():
    """#87: стоп-слово в названии → reason STOPWORD_TITLE в журнале."""
    from hhru_bot.history import SKIP_REASONS

    filters = SearchFilters(text="x", exclude_keywords=["1С"])
    history = FakeHistory()
    filter_candidates([card("1", title="Программист 1С")], filters, "r1", history)
    assert ("r1", "1", SKIP_REASONS.STOPWORD_TITLE) in history.recorded_skips


def test_filter_candidates_does_not_record_clean_cards():
    """#87: чистые кандидаты НЕ пишутся в skipped (только отсев)."""
    history = FakeHistory()
    filter_candidates([card("1")], SearchFilters(text="x"), "r1", history)
    assert history.recorded_skips == []


def test_filter_candidates_skips_cached_skipped_vacancy():
    """#87: кэш is_skipped — вакансия из журнала отсева не проходит дальше.

    Ради экономии LLM/времени (#74/#85): повторный search не пересматривает
    уже отсеянные вакансии. is_skipped срабатывает раньше has_applied/exclude
    и не перезаписывает журнал (запись уже есть — дублировать незачем).
    """
    history = FakeHistory(skipped={("r1", "9")})
    candidates, skipped = filter_candidates([card("9")], SearchFilters(text="x"), "r1", history)
    assert candidates == []
    assert len(skipped) == 1
    assert skipped[0][0].vacancy_id == "9"
    assert history.recorded_skips == []  # кэш-срабатывание не пишет дубль


# --- VacancyCard: поле salary (issue #14) ------------------------------------


def test_vacancy_card_salary_default_none():
    c = card("1")
    assert c.salary is None


def test_vacancy_card_accepts_salary():
    c = VacancyCard(
        vacancy_id="1",
        title="T",
        company="C",
        url="https://hh.ru/vacancy/1",
        salary=SalaryInfo(150000, 200000, "RUB", "150 000–200 000 руб."),
    )
    assert c.salary is not None
    assert c.salary.salary_from == 150000
    assert c.salary.salary_to == 200000


# --- VacancyCard: employer_info + парсинг рейтинга (issue #74) --------------


def test_vacancy_card_employer_info_default_none():
    c = card("1")
    assert c.employer_info is None


def test_parse_rating_comma_decimal_separator():
    # Русская локаль: «4,5» → 4.5. Берётся первый токен.
    from hhru_bot.search import _parse_rating

    assert _parse_rating("4,5") == 4.5
    assert _parse_rating("4.5") == 4.5


def test_parse_rating_handles_garbage():
    from hhru_bot.search import _parse_rating

    assert _parse_rating(None) is None
    assert _parse_rating("") is None
    assert _parse_rating("нет рейтинга") is None


def test_parse_reviews_count_with_separators():
    # «245 отзывов» / «1 245 отзывов» (с разделителем разрядов) → int.
    from hhru_bot.search import _parse_reviews_count

    assert _parse_reviews_count("245 отзывов") == 245
    assert _parse_reviews_count("1\xa0024 отзыва") == 1024  # nbsp-разделитель


def test_parse_reviews_count_none_when_no_number():
    from hhru_bot.search import _parse_reviews_count

    assert _parse_reviews_count(None) is None
    assert _parse_reviews_count("") is None
    assert _parse_reviews_count("нет отзывов") is None


# --- rank_candidates: обратная совместимость без provider (регрессия #74) ----


def test_rank_candidates_without_provider_uses_legacy_heuristic():
    """Без scoring_provider ранжирование = эвристика #15 (поведение не изменилось).

    Регрессия #74: новый опц. параметр не должен сломать существующий путь.
    breakdown не содержит employer_tier (это маркер HeuristicScoringProvider,
    а rank_candidates без provider зовёт _score_card напрямую — как в #15).
    """
    from hhru_bot.config_sections.scoring import ScoringConfig

    filters = SearchFilters(text="python", must_have=["django"])
    cards = [
        VacancyCard(vacancy_id="1", title="Python Developer", company="C", url="u"),
        VacancyCard(vacancy_id="2", title="Python Django Developer", company="C", url="u"),
    ]
    resume = ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/AAA111",
        search=filters,
        scoring=ScoringConfig(),
    )
    ranked = rank_candidates(cards, filters, resume)  # без provider
    order = [c.vacancy_id for c, _s, _b in ranked]
    assert order == ["2", "1"]  # django-матч выше
    _c, _s, breakdown = ranked[0]
    assert "employer_tier" not in breakdown  # эвристика #15 без tier-буста


def test_rank_candidates_with_provider_uses_provider_score():
    """Со scoring_provider score/breakdown берутся из provider.score()."""
    from hhru_bot.scoring import ScoreOutcome

    class _SpyProvider:
        def __init__(self):
            self.called: list[str] = []

        def score(self, card, resume_profile=None):  # noqa: ARG002
            self.called.append(card.vacancy_id)
            # Даём разный score, чтобы проверить сортировку по provider-скору.
            s = 90.0 if card.vacancy_id == "1" else 10.0
            return ScoreOutcome(score_0_100=s, breakdown={"llm": s})

    filters = SearchFilters(text="python")
    cards = [
        VacancyCard(vacancy_id="1", title="A", company="C", url="u"),
        VacancyCard(vacancy_id="2", title="B", company="C", url="u"),
    ]
    resume = ResumeConfig(id="r1", resume_url="https://hh.ru/resume/AAA111", search=filters)
    spy = _SpyProvider()
    ranked = rank_candidates(cards, filters, resume, scoring_provider=spy)
    # Provider вызван на каждую карточку; сортировка по его score (desc).
    assert spy.called == ["1", "2"]
    assert [c.vacancy_id for c, _s, _b in ranked] == ["1", "2"]
    assert ranked[0][2] == {"llm": 90.0}


# --- rank_candidates: shortlist-кэп LLM-запросов (#74 F3, анти-фрод) ---------


def test_rank_candidates_llm_shortlist_caps_provider_calls():
    """F3: с llm_shortlist=K провайдер зовётся только на топ-K по эвристике;
    остальные сохраняют эвристический score. Число LLM-запросов ≤ K."""
    from hhru_bot.config_sections.scoring import ScoringConfig
    from hhru_bot.scoring import ScoreOutcome

    class _CountingProvider:
        def __init__(self):
            self.called: list[str] = []

        def score(self, card, resume_profile=None):  # noqa: ARG002
            self.called.append(card.vacancy_id)
            return ScoreOutcome(score_0_100=50.0, mode="llm", breakdown={"llm": 50.0})

    # Эвристика: title с must_have-матчем выше. must_have=['django'].
    filters = SearchFilters(text="python", must_have=["django"])
    cards = [
        VacancyCard(vacancy_id="no1", title="Python Developer", company="C", url="u"),
        VacancyCard(vacancy_id="yes2", title="Python Django Developer", company="C", url="u"),
        VacancyCard(vacancy_id="no3", title="Python Other", company="C", url="u"),
        VacancyCard(vacancy_id="no4", title="Python Foo", company="C", url="u"),
    ]
    resume = ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/AAA111",
        search=filters,
        scoring=ScoringConfig(),
    )
    provider = _CountingProvider()
    ranked = rank_candidates(cards, filters, resume, scoring_provider=provider, llm_shortlist=1)
    # LLM позван только на 1 карточку — топ эвристики (django-матч 'yes2').
    assert provider.called == ["yes2"]
    # Финальный порядок: yes2 (LLM=50) — но эвристика yes2 была выше; проверим,
    # что остальные карточки сохранили эвристический breakdown (без ключа llm).
    by_id = {c.vacancy_id: b for c, _s, b in ranked}
    assert "llm" in by_id["yes2"]  # LLM-скоринг
    assert "llm" not in by_id["no1"]  # эвристика, LLM не звался
    assert "llm" not in by_id["no4"]


def test_rank_candidates_shortlist_ignored_without_provider():
    """F3: llm_shortlist без провайдера игнорируется — обычная эвристика #15."""
    filters = SearchFilters(text="python")
    cards = [VacancyCard(vacancy_id="1", title="A", company="C", url="u")]
    resume = ResumeConfig(id="r1", resume_url="https://hh.ru/resume/AAA111", search=filters)
    # Не должно падать; llm_shortlist без эффекта.
    ranked = rank_candidates(cards, filters, resume, llm_shortlist=5)
    assert len(ranked) == 1

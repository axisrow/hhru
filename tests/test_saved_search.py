"""Тесты автопоисков hh.ru (#1052): план сохранения, детерминированное имя,
список автопоисков, разбор URL выдачи и боевой путь (email-канал).

Браузера нет — чистые функции + мок Page по паттерну test_report_vacancy_browser.
"""

from __future__ import annotations

from typing import cast

import pytest
from playwright.sync_api import Page as PlaywrightPage
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import hhru_bot.saved_search as ss
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.selector_groups.saved_search import (
    AUTOSEARCH_EMPTY,
    AUTOSEARCH_ITEM,
    AUTOSEARCH_NAME_CHECKBOX,
    AUTOSEARCH_URL_LINK,
    FAVORITES_SEARCHES_TAB,
    SEARCH_SAVE_BUTTON,
    SEARCH_SAVE_CHANNEL_EMAIL,
    SEARCH_SAVE_CREATED,
    SEARCH_SAVE_DROPDOWN,
)

pytestmark = pytest.mark.unit


class Locator:
    """Мок Playwright-локатора с ДИНАМИЧЕСКИМ count (как у настоящего locator:

    результат читается на момент вызова, а не создания — важно для флоу,
    где клик по одному селектору открывает другой экран).
    """

    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def count(self):
        return self.page.counts.get(self.selector, 0)

    @property
    def first(self):
        return self

    def nth(self, i):  # noqa: ARG002 - одна строка списка
        return self

    def locator(self, selector):
        return Locator(self.page, selector)

    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self.count() == 0:
            raise PlaywrightTimeoutError(f"not visible: {self.selector}")

    def get_attribute(self, name):
        attrs = self.page.attrs.get(self.selector)
        return attrs.get(name) if attrs else None

    def click(self):
        self.page.clicks.append(self.selector)
        self.page.on_click(self.selector)


class Page:
    def __init__(self, counts=None, attrs=None):
        self.url = "https://hh.ru/search/vacancy?text=python"
        self.counts = counts if counts is not None else {SEARCH_SAVE_BUTTON: 1}
        self.attrs = attrs or {}
        self.clicks: list[str] = []
        self.login_form = False

    def locator(self, selector):
        return Locator(self, selector)

    def on_click(self, selector):
        """Клик открывает следующий экран флоу сохранения (живой путь 2026-09-08)."""
        if selector == SEARCH_SAVE_BUTTON:
            self.counts[SEARCH_SAVE_DROPDOWN] = 1
            self.counts[SEARCH_SAVE_CHANNEL_EMAIL] = 1
        if selector == SEARCH_SAVE_CHANNEL_EMAIL:
            self.counts[SEARCH_SAVE_CREATED] = 1


# Page.locator ссылается на self через хак выше — упростим прямым конструктором.
def _page(**kwargs) -> Page:
    p = Page(counts=kwargs.get("counts"), attrs=kwargs.get("attrs"))
    return p


def _resume(**kwargs) -> ResumeConfig:
    return ResumeConfig(
        id="00001",
        resume_url="https://hh.ru/resume/00001",
        search=SearchFilters(text="python", **kwargs),
    )


def _patch_navigation(monkeypatch, login_form=False):
    monkeypatch.setattr(ss, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(ss, "has_login_form", lambda page: login_form)


# --- детерминированное имя и ключ ---


def test_name_text_only():
    assert ss.deterministic_saved_search_name(SearchFilters(text="python")) == "python"


def test_name_includes_nonempty_params_only():
    name = ss.deterministic_saved_search_name(
        SearchFilters(text="python", area=1, salary_from=100000, experience="between1And3")
    )
    assert name == "python (area=1, salary=100000, experience=between1And3)"


def test_name_deterministic_for_same_params():
    a = SearchFilters(text="python", area=1)
    b = SearchFilters(text="python", area=1)
    assert ss.deterministic_saved_search_name(a) == ss.deterministic_saved_search_name(b)


def test_query_key_differs_for_different_params():
    assert ss.saved_search_query_key(SearchFilters(text="a")) != ss.saved_search_query_key(
        SearchFilters(text="b")
    )


def test_query_key_ignores_local_only_fields():
    # exclude_keywords и пр. — локальные фильтры CLI, в URL выдачи (и значит в
    # автопоиск hh.ru) не входят; ключ параметрики меняться не должен.
    a = SearchFilters(text="python", exclude_keywords=["x"])
    b = SearchFilters(text="python")
    assert ss.saved_search_query_key(a) == ss.saved_search_query_key(b)


# --- parse_saved_search_url ---


def test_parse_url_takes_search_params_only():
    filters = ss.parse_saved_search_url(
        "https://hh.ru/search/vacancy?text=python&area=1&salary=100000"
        "&experience=between1And3&schedule=fullDay"
        "&saved_search_id=111159979&L_is_autosearch=true&hhtmFrom=favorites"
        "&no_magic=true&page=2"
    )
    assert filters.text == "python"
    assert filters.area == 1
    assert filters.salary_from == 100000
    assert filters.experience == "between1And3"
    assert filters.schedule == "fullDay"


def test_parse_url_without_params_is_bare_text():
    filters = ss.parse_saved_search_url("/search/vacancy?text=qa")
    assert filters.text == "qa"
    assert filters.area is None
    assert filters.salary_from is None


def test_parse_url_tolerates_non_numeric_area():
    filters = ss.parse_saved_search_url("/search/vacancy?text=qa&area=abc")
    assert filters.area is None


# --- save_search_on_hh ---


def test_dry_run_returns_plan_without_clicks(monkeypatch):
    _patch_navigation(monkeypatch)
    page = _page()
    result = ss.save_search_on_hh(cast(PlaywrightPage, page), _resume(area=1), None, dry_run=True)
    assert result.success is True
    assert result.acted is False
    assert page.clicks == []
    assert "[DRY-RUN]" in result.reason
    assert "area=1" in result.reason
    assert result.name == "python (area=1)"


def test_dry_run_uses_explicit_name(monkeypatch):
    _patch_navigation(monkeypatch)
    result = ss.save_search_on_hh(
        cast(PlaywrightPage, _page()), _resume(), "Мой автопоиск", dry_run=True
    )
    assert result.name == "Мой автопоиск"


def test_missing_button_fails_closed(monkeypatch):
    # count=0: wait_for(visible) первым исчерпывает бюджет гидратации и
    # только затем отказ — порядок «wait, потом count» (#858).
    _patch_navigation(monkeypatch)
    page = _page(counts={SEARCH_SAVE_BUTTON: 0})
    result = ss.save_search_on_hh(cast(PlaywrightPage, page), _resume(), None, dry_run=True)
    assert result.success is False
    assert "не подтвердилась" in result.reason


def test_ambiguous_button_fails_closed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = _page(counts={SEARCH_SAVE_BUTTON: 2})
    result = ss.save_search_on_hh(cast(PlaywrightPage, page), _resume(), None, dry_run=True)
    assert result.success is False
    assert "неоднозначна" in result.reason


def test_not_authenticated_raises(monkeypatch):
    _patch_navigation(monkeypatch, login_form=True)
    with pytest.raises(ss.NotAuthenticated):
        ss.save_search_on_hh(cast(PlaywrightPage, _page()), _resume(), None, dry_run=True)


def test_already_saved_is_skip_without_clicks(monkeypatch):
    """Дубль: кнопка в состоянии «Сохранён» — честный skip до всякого клика."""
    _patch_navigation(monkeypatch)
    page = _page(counts={SEARCH_SAVE_BUTTON: 0, SEARCH_SAVE_CREATED: 1})
    result = ss.save_search_on_hh(cast(PlaywrightPage, page), _resume(), None, dry_run=False)
    assert result.success is False
    assert result.skipped is True
    assert result.uncertain is False
    assert page.clicks == []
    assert "уже сохранён" in result.reason


def test_force_saves_via_email_channel(monkeypatch):
    """Боевой путь живого прогона 2026-09-08: кнопка → tooltip → «На почту» → «Сохранён»."""
    _patch_navigation(monkeypatch)
    page = _page()
    calls = []
    result = ss.save_search_on_hh(
        cast(PlaywrightPage, page),
        _resume(),
        None,
        dry_run=False,
        before_click=lambda: calls.append("before_click"),
    )
    assert result.success is True
    assert result.acted is True
    assert page.clicks == [SEARCH_SAVE_BUTTON, SEARCH_SAVE_CHANNEL_EMAIL]
    # seam DurableMutationAttempt стоит вплотную к мутирующему клику канала.
    assert calls == ["before_click"]


def test_force_timeout_after_channel_click_is_uncertain(monkeypatch):
    """Таймаут «Сохранён» после клика канала — клик мог уйти (#176/#207)."""

    class _NoCreatedPage(Page):
        def on_click(self, selector):
            if selector == SEARCH_SAVE_BUTTON:
                self.counts[SEARCH_SAVE_DROPDOWN] = 1
                self.counts[SEARCH_SAVE_CHANNEL_EMAIL] = 1
            # клик канала НЕ переводит кнопку в «Сохранён»

    _patch_navigation(monkeypatch)
    page = _NoCreatedPage(counts={SEARCH_SAVE_BUTTON: 1})
    result = ss.save_search_on_hh(cast(PlaywrightPage, page), _resume(), None, dry_run=False)
    assert result.success is False
    assert result.acted is True
    assert result.uncertain is True
    assert SEARCH_SAVE_CHANNEL_EMAIL in page.clicks


def test_force_tooltip_failure_before_channel_click_is_plain_failed(monkeypatch):
    """Таймаут открытия tooltip ДО клика канала — обычный failed, не uncertain.

    Контракт #176: uncertain только после мутирующего клика; здесь клик
    канала заведомо не произошёл (два try-скоупа вокруг before_click).
    """

    class _NoTooltipPage(Page):
        def on_click(self, selector):
            pass  # клик по кнопке не открывает tooltip

    _patch_navigation(monkeypatch)
    page = _NoTooltipPage(counts={SEARCH_SAVE_BUTTON: 1})
    calls = []
    result = ss.save_search_on_hh(
        cast(PlaywrightPage, page),
        _resume(),
        None,
        dry_run=False,
        before_click=lambda: calls.append("before_click"),
    )
    assert result.success is False
    assert result.acted is False
    assert result.uncertain is False
    assert SEARCH_SAVE_CHANNEL_EMAIL not in page.clicks
    assert calls == []
    assert "клик канала не выполнен" in result.reason


# --- list_saved_searches ---


def test_find_duplicate_by_url_params():
    entries = [
        ss.SavedSearchEntry(
            name="python",
            url="/search/vacancy?text=python&area=1&saved_search_id=111159979&hhtmFrom=favorites",
        ),
        ss.SavedSearchEntry(name="другой", url="/search/vacancy?text=qa"),
    ]
    # Сервисные параметры (saved_search_id, hhtmFrom) в сравнении не участвуют.
    dup = ss.find_duplicate_saved_search(entries, SearchFilters(text="python", area=1))
    assert dup is not None and dup.name == "python"
    assert ss.find_duplicate_saved_search(entries, SearchFilters(text="python")) is None
    assert ss.find_duplicate_saved_search(entries, SearchFilters(text="qa")) is not None


def test_list_empty_state_confirmed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = _page(counts={FAVORITES_SEARCHES_TAB: 1, AUTOSEARCH_EMPTY: 1, AUTOSEARCH_ITEM: 0})
    assert ss.list_saved_searches(cast(PlaywrightPage, page)) == []


def test_list_parses_rows(monkeypatch):
    _patch_navigation(monkeypatch)
    page = _page(
        counts={FAVORITES_SEARCHES_TAB: 1, AUTOSEARCH_EMPTY: 0, AUTOSEARCH_ITEM: 1},
        attrs={
            AUTOSEARCH_NAME_CHECKBOX: {"aria-label": "python"},
            AUTOSEARCH_URL_LINK: {
                "href": "/search/vacancy?text=python&area=1&saved_search_id=111159979"
            },
        },
    )
    entries = ss.list_saved_searches(cast(PlaywrightPage, page))
    assert len(entries) == 1
    assert entries[0].name == "python"
    assert entries[0].url is not None
    assert "text=python" in entries[0].url


def test_list_row_without_name_is_indeterminate(monkeypatch):
    _patch_navigation(monkeypatch)
    page = _page(
        counts={FAVORITES_SEARCHES_TAB: 1, AUTOSEARCH_EMPTY: 0, AUTOSEARCH_ITEM: 1},
        attrs={AUTOSEARCH_NAME_CHECKBOX: {}},
    )
    with pytest.raises(ss.SavedSearchListIndeterminate):
        ss.list_saved_searches(cast(PlaywrightPage, page))


def test_list_neither_rows_nor_empty_is_indeterminate(monkeypatch):
    """Ни строк, ни пустого состояния — не «пусто», а непрочитанное (#464)."""
    _patch_navigation(monkeypatch)
    page = _page(counts={FAVORITES_SEARCHES_TAB: 1, AUTOSEARCH_EMPTY: 0, AUTOSEARCH_ITEM: 0})
    with pytest.raises(ss.SavedSearchListIndeterminate):
        ss.list_saved_searches(cast(PlaywrightPage, page))


def test_list_tab_not_rendered_is_indeterminate(monkeypatch):
    _patch_navigation(monkeypatch)
    page = _page(counts={FAVORITES_SEARCHES_TAB: 0})
    with pytest.raises(ss.SavedSearchListIndeterminate):
        ss.list_saved_searches(cast(PlaywrightPage, page))


def test_list_not_authenticated_raises(monkeypatch):
    _patch_navigation(monkeypatch, login_form=True)
    with pytest.raises(ss.NotAuthenticated):
        ss.list_saved_searches(cast(PlaywrightPage, _page()))


# --- режимы команды search (#1052) ---


def _mode_args(**overrides):
    import argparse

    defaults = {"list_saved": False, "saved": None, "save": False, "force": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_save_with_saved_is_rejected(capsys, monkeypatch):
    from hhru_bot.commands.search import _run_saved_search_modes

    class _FakeContext:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def new_page(self):
            raise AssertionError("браузер не должен открываться для ошибок сочетания флагов")

    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **k: _FakeContext())
    failed = _run_saved_search_modes(_mode_args(save=True, saved="x"), object())
    assert failed is True
    assert "[FAIL] --save не сочетается с --saved" in capsys.readouterr().out

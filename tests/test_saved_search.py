"""Тесты автопоисков hh.ru (#1052): план сохранения, детерминированное имя,
список автопоисков и fail-closed границы неподтверждённого попапа.

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
    FAVORITES_SEARCHES_TAB,
    SEARCH_SAVE_BUTTON,
)

pytestmark = pytest.mark.unit


class Locator:
    def __init__(self, page, selector, count=1):
        self.page = page
        self.selector = selector
        self._count = count

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def wait_for(self, *, state, timeout):  # noqa: ARG002
        if self._count == 0:
            raise PlaywrightTimeoutError(f"not visible: {self.selector}")

    def click(self):
        self.page.clicks.append(self.selector)


class Page:
    def __init__(self, counts=None):
        self.url = "https://hh.ru/search/vacancy?text=python"
        self.counts = counts or {}
        self.clicks: list[str] = []
        self.login_form = False

    def locator(self, selector):
        return Locator(self, selector, count=self.counts.get(selector, 1))


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


# --- save_search_on_hh ---


def test_dry_run_returns_plan_without_clicks(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page()
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
        cast(PlaywrightPage, Page()), _resume(), "Мой автопоиск", dry_run=True
    )
    assert result.name == "Мой автопоиск"


def test_missing_button_fails_closed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(counts={SEARCH_SAVE_BUTTON: 0})
    result = ss.save_search_on_hh(cast(PlaywrightPage, page), _resume(), None, dry_run=True)
    assert result.success is False
    assert "не найдена" in result.reason


def test_ambiguous_button_fails_closed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(counts={SEARCH_SAVE_BUTTON: 2})
    result = ss.save_search_on_hh(cast(PlaywrightPage, page), _resume(), None, dry_run=True)
    assert result.success is False
    assert "неоднозначна" in result.reason


def test_not_authenticated_raises(monkeypatch):
    _patch_navigation(monkeypatch, login_form=True)
    with pytest.raises(ss.NotAuthenticated):
        ss.save_search_on_hh(cast(PlaywrightPage, Page()), _resume(), None, dry_run=True)


def test_force_refuses_to_click_until_popup_confirmed(monkeypatch):
    """Боевой путь fail-closed: попап сохранения не подтверждён живым DOM.

    Тот же контракт, что у report-vacancy (#745): неподтверждённый
    мутационный клик не выполняется, исход — обычный failed без uncertain
    (клика не было, hh.ru не мутирован).
    """
    _patch_navigation(monkeypatch)
    page = Page()
    result = ss.save_search_on_hh(cast(PlaywrightPage, page), _resume(), None, dry_run=False)
    assert result.success is False
    assert result.acted is False
    assert result.uncertain is False
    assert page.clicks == []
    assert "не подтверждены живым DOM" in result.reason


# --- list_saved_searches ---


def test_list_empty_state_confirmed(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(counts={AUTOSEARCH_EMPTY: 1})
    assert ss.list_saved_searches(cast(PlaywrightPage, page)) == []


def test_list_nonempty_is_indeterminate(monkeypatch):
    """Непустой список без подтверждённых селекторов строк — не «пусто» (#464)."""
    _patch_navigation(monkeypatch)
    page = Page(counts={AUTOSEARCH_EMPTY: 0})
    with pytest.raises(ss.SavedSearchListIndeterminate):
        ss.list_saved_searches(cast(PlaywrightPage, page))


def test_list_tab_not_rendered_is_indeterminate(monkeypatch):
    _patch_navigation(monkeypatch)
    page = Page(counts={FAVORITES_SEARCHES_TAB: 0})
    with pytest.raises(ss.SavedSearchListIndeterminate):
        ss.list_saved_searches(cast(PlaywrightPage, page))


def test_list_not_authenticated_raises(monkeypatch):
    _patch_navigation(monkeypatch, login_form=True)
    with pytest.raises(ss.NotAuthenticated):
        ss.list_saved_searches(cast(PlaywrightPage, Page()))

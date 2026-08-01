"""Тесты браузерного шага copy_resume_on_hh (#116) — стабы Page, без браузера.

Сценарии: dry-run не кликает; карточка не найдена/неоднозначна — fail-closed;
кнопка «Дублировать» не отрендерилась (лимит резюме hh.ru) — fail; успех через
смену URL; fallback через diff списка резюме; hh.ru не создал копию — fail.
"""

from __future__ import annotations

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import hhru_bot.copy_resume as cr
from hhru_bot.config import ResumeConfig, SearchFilters
from hhru_bot.selector_groups.resume_list import (
    RESUME_DUPLICATE_INLINE,
    RESUME_DUPLICATE_MENU_ITEM,
    RESUME_LIST_ACTION_MORE,
    RESUME_LIST_CARD,
    RESUME_LIST_CARD_LINK_TPL,
)

OLD_ID = "a" * 38
NEW_ID = "b" * 38

CARD_SEL = f"{RESUME_LIST_CARD}:has({RESUME_LIST_CARD_LINK_TPL.format(resume_id=OLD_ID)})"
DUP_SEL = f"{RESUME_DUPLICATE_MENU_ITEM}, {RESUME_DUPLICATE_INLINE}"


def _resume() -> ResumeConfig:
    return ResumeConfig(
        id="backend",
        resume_url=f"https://hh.ru/resume/{OLD_ID}",
        search=SearchFilters(text="python"),
    )


class StubLocator:
    def __init__(self, page, selector, count=1, wait_ok=True):
        self._page = page
        self.selector = selector
        self._count = count
        self._wait_ok = wait_ok

    def count(self):
        return self._count

    def wait_for(self, timeout=None):
        if not self._wait_ok:
            raise PlaywrightTimeoutError(f"timeout: {self.selector}")

    def click(self):
        self._page.clicks.append(self.selector)
        self._page.on_click(self.selector)

    def locator(self, selector):
        return self._page.locator(selector)

    @property
    def first(self):
        return self

    def all(self):
        return [self]

    def get_attribute(self, name):
        return None


class StubPage:
    """Конфигурируемый минимум Page для copy_resume_on_hh.

    ``locators`` — переопределения по селектору; ``card_hashes`` — что вернёт
    сбор resume-card-link-* (список множеств: по одному на каждый вызов);
    ``url_after_click`` — куда «переходит» страница после клика по «Дублировать».
    """

    def __init__(self, locators=None, card_hashes=None, url_after_click=None):
        self._locators = locators or {}
        self._card_hashes = list(card_hashes or [])
        self._url_after_click = url_after_click
        self.url = cr.RESUMES_LIST_URL
        self.clicks = []
        self.gotos = []

    def locator(self, selector):
        if selector in self._locators:
            return self._locators[selector]
        if selector.startswith("[data-qa^='resume-card-link-'"):
            raise AssertionError("сбор хэшей должен идти через _card_hashes")
        return StubLocator(self, selector)

    def on_click(self, selector):
        if selector == DUP_SEL and self._url_after_click:
            self.url = self._url_after_click

    def wait_for_url(self, pattern, timeout=None):
        if not pattern.search(self.url):
            raise PlaywrightTimeoutError("wait_for_url timeout")


def _patch_env(monkeypatch, page):
    monkeypatch.setattr(cr, "goto_hh", lambda p, url, **kw: page.gotos.append(url))
    monkeypatch.setattr(
        cr, "_card_hashes", lambda p: page._card_hashes.pop(0) if page._card_hashes else set()
    )


def test_dry_run_does_not_click(monkeypatch):
    page = StubPage()
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=True)
    assert result.success
    assert page.clicks == []
    assert page.gotos == [cr.RESUMES_LIST_URL]


def test_card_not_found_fails_closed(monkeypatch):
    page = StubPage()
    page._locators[CARD_SEL] = StubLocator(page, CARD_SEL, count=0, wait_ok=False)
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert "не найдено" in result.reason
    assert page.clicks == []


def test_ambiguous_card_fails_closed(monkeypatch):
    page = StubPage()
    page._locators[CARD_SEL] = StubLocator(page, CARD_SEL, count=2)
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert page.clicks == []


def test_duplicate_button_missing_fails(monkeypatch):
    page = StubPage()
    page._locators[DUP_SEL] = StubLocator(page, DUP_SEL, wait_ok=False)
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert "Дублировать" in result.reason
    # Меню открыли, но само дублирование не кликали.
    assert page.clicks == [RESUME_LIST_ACTION_MORE]


def test_success_via_url_navigation(monkeypatch):
    page = StubPage(url_after_click=f"https://hh.ru/resume/{NEW_ID}?query=1")
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert result.success
    assert result.new_resume_id == NEW_ID
    assert page.clicks == [RESUME_LIST_ACTION_MORE, DUP_SEL]


def test_url_shows_old_id_falls_back_to_list_diff(monkeypatch):
    # Навигация привела на URL исходного резюме — новый id берём из diff списка.
    page = StubPage(
        url_after_click=f"https://hh.ru/resume/{OLD_ID}",
        card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}],
    )
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert result.success
    assert result.new_resume_id == NEW_ID
    # Список перезагружали для diff.
    assert page.gotos == [cr.RESUMES_LIST_URL, cr.RESUMES_LIST_URL]


def test_no_navigation_and_no_new_card_fails(monkeypatch):
    # POST не прошёл: URL не изменился, в списке ничего нового — fail-closed.
    page = StubPage(card_hashes=[{OLD_ID}, {OLD_ID}])
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert result.new_resume_id == ""


def test_multiple_new_cards_ambiguous_fails(monkeypatch):
    # Diff дал два новых хэша — определить копию нельзя, fail-closed.
    page = StubPage(card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID, "c" * 38}])
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success

"""Тесты браузерного шага copy_resume_on_hh (#116) — стабы Page, без браузера.

Сценарии: dry-run не кликает; карточка не найдена/неоднозначна — fail-closed;
кнопка «Дублировать» не отрендерилась (лимит резюме hh.ru) — fail; кнопка
неоднозначна внутри карточки — fail-closed; успех через смену URL; fallback
через diff списка резюме; hh.ru не создал копию — fail.

Стабы моделируют строгую (strict-mode) семантику настоящего Playwright:
``count() != 1`` при вызове ``wait_for()``/``click()`` кидает
``playwright.sync_api.Error`` (НЕ ``TimeoutError``) — именно поэтому
``copy_resume_on_hh`` обязан звать ``count()`` ДО ``wait_for``/``click``,
а не полагаться на try/except TimeoutError. ``.first`` возвращает
самостоятельный однозначный локатор — не self.
"""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import hhru_bot.copy_resume as cr
from hhru_bot.browser import LOGIN_FORM
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
    """Строгий (strict-mode) стаб Playwright Locator.

    ``count`` — сколько элементов резолвит селектор ПРЯМО СЕЙЧАС (без ожидания).
    ``count_after_wait`` — сколько станет после ``wait_for`` (по умолчанию =
    ``count``, т.е. состояние не меняется). ``wait_ok=False`` — таймаут
    (элемент так и не появился, ``count``/``count_after_wait`` == 0).
    """

    def __init__(self, page, selector, count=1, count_after_wait=None, wait_ok=True, scope=""):
        self._page = page
        self.selector = selector
        self._count = count
        self._count_after_wait = count if count_after_wait is None else count_after_wait
        self._wait_ok = wait_ok
        self._scope = scope  # непусто у локаторов, полученных через card.locator(...)

    def count(self):
        return self._count

    def _resolve(self):
        if not self._wait_ok:
            raise PlaywrightTimeoutError(f"timeout: {self.selector}")
        n = self._count_after_wait
        if n != 1:
            raise PlaywrightError(
                f"strict mode violation: {self.selector} resolved to {n} elements"
            )
        self._count = n

    def wait_for(self, timeout=None):
        self._resolve()

    def click(self):
        self._resolve()
        self._page.clicks.append((self._scope, self.selector))
        self._page.on_click(self._scope, self.selector)

    def locator(self, selector):
        return self._page.card_scoped_locator(self._scope or self.selector, selector)

    @property
    def first(self):
        one = StubLocator(self._page, self.selector, count=min(self._count, 1), scope=self._scope)
        one._count_after_wait = 1
        one._wait_ok = self._wait_ok
        return one

    def all(self):
        return [self]

    def get_attribute(self, name):
        return None


class StubPage:
    """Конфигурируемый минимум Page для copy_resume_on_hh.

    ``card_locators`` — переопределения для локатора карточки (по CARD_SEL).
    ``dup_locators`` — переопределения кнопки «Дублировать» ПО СКОУПУ карточки
    (``{card_selector: StubLocator}``) — имитирует, что card.locator(...) ищет
    только внутри своей карточки, а не по всей странице.
    ``card_hashes`` — что вернёт сбор resume-card-link-* (список множеств: по
    одному на каждый вызов); ``url_after_click`` — куда «переходит» страница
    после клика по «Дублировать».
    """

    def __init__(
        self,
        card_locator=None,
        dup_locators=None,
        card_hashes=None,
        url_after_click=None,
    ):
        self._card_locator = card_locator
        self._dup_locators = dup_locators or {}
        self._card_hashes = list(card_hashes or [])
        self._url_after_click = url_after_click
        self.url = cr.RESUMES_LIST_URL
        self.clicks = []
        self.gotos = []

    def locator(self, selector):
        if selector == LOGIN_FORM:
            return StubLocator(self, selector, count=0)
        if selector == CARD_SEL:
            return self._card_locator or StubLocator(self, selector)
        if selector.startswith("[data-qa^='resume-card-link-'"):
            raise AssertionError("сбор хэшей должен идти через _card_hashes")
        raise AssertionError(f"неожиданный page.locator: {selector}")

    def card_scoped_locator(self, card_scope, selector):
        if selector == RESUME_LIST_ACTION_MORE:
            return StubLocator(self, selector, scope=card_scope)
        if selector == DUP_SEL:
            found = self._dup_locators.get(card_scope)
            if found is not None:
                found._page = self
                return found
            return StubLocator(self, selector, scope=card_scope)
        raise AssertionError(f"неожиданный card.locator: {selector}")

    def on_click(self, scope, selector):
        if selector == DUP_SEL and self._url_after_click:
            self.url = self._url_after_click

    def wait_for_url(self, pattern, timeout=None):
        if not pattern.search(self.url):
            raise PlaywrightTimeoutError("wait_for_url timeout")


def _patch_env(monkeypatch, page):
    # Захватываем kwargs (ready_selector), чтобы тест мог проверить, что goto_hh
    # вызывается с ready_selector=RESUME_LIST_CARD (#142).
    page.goto_kwargs = []

    def _goto(p, url, **kw):
        page.gotos.append(url)
        page.goto_kwargs.append(kw)

    monkeypatch.setattr(cr, "goto_hh", _goto)
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


def test_goto_hh_does_not_wait_for_card_before_login_check(monkeypatch):
    """Форма входа должна проверяться до ожидания карточки и её ретраев."""
    page = StubPage(
        url_after_click=f"https://hh.ru/resume/{OLD_ID}", card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}]
    )
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert result.success
    # fallback-путь: goto_hh вызван дважды — обе с ready_selector
    assert page.gotos == [cr.RESUMES_LIST_URL, cr.RESUMES_LIST_URL]
    assert all("ready_selector" not in kw for kw in page.goto_kwargs)


def test_card_not_found_fails_closed(monkeypatch):
    page = StubPage(card_locator=StubLocator(None, CARD_SEL, count=0, wait_ok=False))
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert "не найдено" in result.reason
    assert page.clicks == []


def test_ambiguous_card_fails_closed(monkeypatch):
    # count() сразу возвращает 2 — неоднозначность ловится ДО wait_for/click
    # (strict mode на реальном Playwright кинул бы Error, не TimeoutError).
    page = StubPage(card_locator=StubLocator(None, CARD_SEL, count=2))
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert "неоднозначно" in result.reason
    assert page.clicks == []


def test_duplicate_button_missing_fails(monkeypatch):
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=0, wait_ok=False)})
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert "Дублировать" in result.reason
    # Меню открыли, но само дублирование не кликали.
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE)]


def test_duplicate_button_ambiguous_within_card_fails_closed(monkeypatch):
    # Внутри карточки нашлось 2 совпадения (баг разметки) — не гадаем, fail-closed.
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=2)})
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert "неоднозначно" in result.reason
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE)]


def test_success_via_url_navigation(monkeypatch):
    page = StubPage(url_after_click=f"https://hh.ru/resume/{NEW_ID}?query=1")
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert result.success
    assert result.new_resume_id == NEW_ID
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE), (CARD_SEL, DUP_SEL)]


def test_duplicate_click_scoped_to_correct_card_not_first_on_page(monkeypatch):
    # Регрессия: на странице два резюме, у второй карточки тоже есть инлайн-кнопка
    # «Дублировать». Клик обязан уйти в кнопку СВОЕЙ карточки (CARD_SEL), а не в
    # первую попавшуюся на странице — иначе можно скопировать чужое резюме.
    other_card_sel = "OTHER_CARD_SEL"
    page = StubPage(
        dup_locators={
            CARD_SEL: StubLocator(None, DUP_SEL, scope=CARD_SEL),
            other_card_sel: StubLocator(None, DUP_SEL, scope=other_card_sel),
        },
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert result.success
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE), (CARD_SEL, DUP_SEL)]


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

"""Тесты браузерного шага copy_resume_on_hh (#116) — стабы Page, без браузера.

Сценарии: dry-run не кликает; карточка не найдена/неоднозначна — fail-closed;
кнопка «Дублировать» не отрендерилась (лимит резюме hh.ru) — fail; кнопка
неоднозначна в portal-меню — fail-closed; успех через смену URL; fallback
через diff списка резюме; hh.ru не создал копию — fail.

Стабы моделируют строгую (strict-mode) семантику настоящего Playwright:
``count() != 1`` при вызове ``wait_for()``/``click()`` кидает
``playwright.sync_api.Error`` (НЕ ``TimeoutError``) — именно поэтому
``copy_resume_on_hh`` обязан звать ``count()`` ДО ``wait_for``/``click``,
а не полагаться на try/except TimeoutError. ``.first`` возвращает
самостоятельный однозначный локатор — не self.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
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
    RESUME_PROFILE_READY,
)

pytestmark = pytest.mark.integration

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
        if self.selector == RESUME_PROFILE_READY and self._page is not None:
            return self._page.ready_count
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

    def wait_for(self, *, timeout=None):
        if self._page is not None:
            self._page.wait_timeouts.append((self._scope, self.selector, timeout))
        self._resolve()

    def click(self, *, timeout=None):
        self._resolve()
        self._page.clicks.append((self._scope, self.selector))
        self._page.on_click(self._scope, self.selector)

    def locator(self, selector):
        return self._page.card_scoped_locator(self._scope or self.selector, selector)

    def or_(self, other):
        if {self.selector, other.selector} == {
            RESUME_DUPLICATE_MENU_ITEM,
            RESUME_DUPLICATE_INLINE,
        }:
            return self._page.locator(DUP_SEL)
        raise AssertionError(f"неожиданный locator.or_: {self.selector}, {other.selector}")

    @property
    def first(self):
        one = StubLocator(self._page, self.selector, count=min(self._count, 1), scope=self._scope)
        one._count_after_wait = 1
        one._wait_ok = self._wait_ok
        one._disabled = getattr(self, "_disabled", False)
        return one

    def all(self):
        return [self]

    def get_attribute(self, name):
        return None

    def is_disabled(self):
        return getattr(self, "_disabled", False)


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
        self.wait_timeouts = []
        self.reloads = []
        self.ready_count = 1
        self.now = 0.0
        self.tick_actions = []
        self.goto_action = None
        self.reload_action = None
        self._event_handlers = {}

    def locator(self, selector):
        if selector == LOGIN_FORM:
            return StubLocator(self, selector, count=0)
        if selector == RESUME_LIST_CARD:
            return StubLocator(self, selector, count=1)
        if selector == CARD_SEL:
            return self._card_locator or StubLocator(self, selector)
        if selector == RESUME_PROFILE_READY:
            return StubLocator(self, selector, count=self.ready_count)
        if selector == RESUME_DUPLICATE_MENU_ITEM:
            return StubLocator(self, selector, count=0)
        if selector == DUP_SEL:
            found = self._dup_locators.get(CARD_SEL)
            if found is not None:
                found._page = self
                found._scope = ""
                return found
            return StubLocator(self, selector)
        if selector.startswith("[data-qa^='resume-card-link-'"):
            raise AssertionError("сбор хэшей должен идти через _card_hashes")
        raise AssertionError(f"неожиданный page.locator: {selector}")

    def card_scoped_locator(self, card_scope, selector):
        if selector == RESUME_LIST_ACTION_MORE:
            return StubLocator(self, selector, scope=card_scope)
        if selector == RESUME_PROFILE_READY:
            return StubLocator(self, selector, count=self.ready_count, scope=card_scope)
        if selector == RESUME_DUPLICATE_INLINE:
            return StubLocator(self, selector, count=0, scope=card_scope)
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

    def wait_for_url(self, pattern, *, wait_until=None, timeout=None):
        assert wait_until == "commit"
        if not pattern.search(self.url):
            raise PlaywrightTimeoutError("wait_for_url timeout")

    def on(self, event, handler):
        self._event_handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        # #749 code-review round 1, finding #3: goto_hh registers a
        # response-listener per attempt and always removes it in a
        # `finally` (including the success path). A Page double with `on`
        # but no `remove_listener` blows up with an uncaught AttributeError
        # the first time a test exercises the real goto_hh through it.
        handlers = self._event_handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def emit(self, event, value):
        for handler in self._event_handlers.get(event, []):
            handler(value)

    def wait_for_timeout(self, timeout):
        self.now += timeout / 1000
        if self.tick_actions:
            self.tick_actions.pop(0)()

    def reload(self, *, wait_until=None):
        self.reloads.append(wait_until)
        if self.reload_action is not None:
            self.reload_action()


def test_real_goto_hh_survives_stub_page_without_remove_listener_bug(monkeypatch):
    """#749 code-review round 1, finding #3 (regression guard).

    StubPage is the one Page double in this suite that defines ``on`` for
    transient-overlay wiring without also defining ``remove_listener``.
    Every other test in this file bypasses this by having ``_patch_env``
    replace ``cr.goto_hh`` with a fake BEFORE any ``original_goto = cr.goto_hh``
    capture runs, so the real ``goto_hh`` never actually touches StubPage in
    those tests. This test calls the real ``browser.goto_hh`` directly,
    closing that gap: StubPage must expose a working ``remove_listener`` (see
    the method above) or this raises AttributeError from goto_hh's `finally`
    on the ordinary success path.
    """
    from hhru_bot.browser import goto_hh

    page = StubPage()
    page.goto = lambda url, **kwargs: page.gotos.append(url)  # type: ignore[attr-defined]

    goto_hh(page, "https://hh.ru/applicant/my_resumes")

    assert page.gotos == ["https://hh.ru/applicant/my_resumes"]


class StubConsoleMessage:
    def __init__(self, text):
        self.text = text


class StubRequest:
    def __init__(self, url):
        self.url = url


class StubResponse(StubRequest):
    def __init__(self, url, status):
        super().__init__(url)
        self.status = status


def _patch_env(monkeypatch, page):
    # Захватываем kwargs (ready_selector), чтобы тест мог проверить, что goto_hh
    # вызывается с ready_selector=RESUME_LIST_CARD (#142).
    page.goto_kwargs = []

    def _goto(p, url, **kw):
        page.gotos.append(url)
        page.goto_kwargs.append(kw)
        if page.goto_action is not None:
            page.goto_action()

    monkeypatch.setattr(cr, "goto_hh", _goto)
    monkeypatch.setattr(
        cr, "_card_hashes", lambda p: page._card_hashes.pop(0) if page._card_hashes else set()
    )
    monkeypatch.setattr(
        cr,
        "_resume_lineage",
        lambda p: {
            OLD_ID: cr.ResumeLineage("100", ""),
            NEW_ID: cr.ResumeLineage("101", "100"),
        },
    )
    monkeypatch.setattr(cr, "_monotonic", lambda: page.now)


def test_dry_run_does_not_click(monkeypatch):
    page = StubPage()
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=True)
    assert result.success
    assert page.clicks == []
    assert page.gotos == [cr.RESUMES_LIST_URL]


def test_duplicate_click_error_is_uncertain(monkeypatch):
    class ErrorDuplicate(StubLocator):
        @property
        def first(self):
            return self

        def click(self, *, timeout=None):
            raise PlaywrightError("navigation interrupted")

    page = StubPage(
        dup_locators={
            "": ErrorDuplicate(None, DUP_SEL),
            CARD_SEL: ErrorDuplicate(None, DUP_SEL),
        }
    )
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert result.success is False
    assert result.uncertain is True
    assert "не подтверждено" in result.reason


def test_goto_hh_ready_selector_and_login_check_order(monkeypatch):
    """#153: auth probe is fast, then fallback waits for rendered cards (#142)."""
    page = StubPage(
        url_after_click=f"https://hh.ru/resume/{NEW_ID}", card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}]
    )
    _patch_env(monkeypatch, page)
    page.events = []
    original_goto = cr.goto_hh

    def _goto(p, url, **kw):
        page.events.append("goto")
        original_goto(p, url, **kw)

    monkeypatch.setattr(cr, "goto_hh", _goto)
    monkeypatch.setattr(cr, "has_login_form", lambda p: page.events.append("auth") or False)
    original_ready = cr._wait_resume_list_ready
    monkeypatch.setattr(
        cr,
        "_wait_resume_list_ready",
        lambda p: (page.events.append("ready"), original_ready(p))[1],
    )
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert result.success
    # fallback-путь: goto_hh вызван дважды — auth probe follows each navigation.
    assert page.gotos == [cr.RESUMES_LIST_URL, cr.RESUMES_LIST_URL]
    assert page.events == ["goto", "auth", "goto", "auth", "ready"]
    assert all("ready_selector" not in kw for kw in page.goto_kwargs)


def test_post_click_login_form_reports_unconfirmed_state(monkeypatch):
    """Codex adversarial review (PR #158): session revoked AFTER the clone
    click must not be reported with ordinary pre-write wording — the click
    already fired the clone POST, so the operator needs to know the copy's
    state is unconfirmed before retrying (regression for the finding)."""
    page = StubPage(url_after_click=f"https://hh.ru/resume/{OLD_ID}", card_hashes=[{OLD_ID}])
    _patch_env(monkeypatch, page)
    goto_calls = []
    original_goto = cr.goto_hh

    def _goto(p, url, **kw):
        goto_calls.append(url)
        original_goto(p, url, **kw)

    monkeypatch.setattr(cr, "goto_hh", _goto)
    # Login form absent on the first (pre-click) navigation, present on the
    # second (fallback, post-click) navigation.
    monkeypatch.setattr(cr, "has_login_form", lambda p: len(goto_calls) >= 2)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert len(goto_calls) == 2  # pre-click goto + fallback goto after the click
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE), ("", DUP_SEL)]
    assert page.reloads == []  # после WRITE-клика recovery категорически запрещён
    # Неопределённое состояние после WRITE-клика → uncertain (а не обычный
    # failed): копия могла создаться, повтор не должен выглядеть безопасным.
    assert result.success is False
    assert result.uncertain is True
    assert "НЕ подтверждено" in result.reason
    assert "уже создана" in result.reason


def test_pre_click_login_form_reports_ordinary_failure(monkeypatch):
    """Sanity check: the pre-write call site keeps the plain pre-write wording
    (no false claim that a copy may already exist)."""
    page = StubPage()
    _patch_env(monkeypatch, page)
    monkeypatch.setattr(cr, "has_login_form", lambda p: True)

    with pytest.raises(cr.NotAuthenticated) as exc_info:
        cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert page.clicks == []
    message = str(exc_info.value)
    assert "НЕ подтверждено" not in message
    assert "уже создана" not in message


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
    assert "квоту прочитать не удалось" in result.reason
    # Client render подтверждён optional-маркером, но отсутствие action не
    # доказывает лимит: quota read мог завершиться сетевым/DOM-сбоем.
    assert page.reloads == []
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE)]


def test_disabled_duplicate_button_reports_unreadable_quota_without_retry(monkeypatch):
    duplicate = StubLocator(None, DUP_SEL, count=1, scope="")
    duplicate._disabled = True
    page = StubPage(dup_locators={CARD_SEL: duplicate})
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert not result.uncertain
    assert "квоту прочитать не удалось" in result.reason
    assert page.reloads == []
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE)]


def test_duplicate_button_can_appear_after_menu_poll(monkeypatch):
    """Карточка SSR готова раньше, чем hydration дорисует actions hh.ru."""
    duplicate = StubLocator(None, DUP_SEL, count=0, scope=CARD_SEL)
    page = StubPage(
        dup_locators={CARD_SEL: duplicate},
        card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    page.tick_actions = [lambda: setattr(duplicate, "_count", 1)]
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert result.success
    assert result.new_resume_id == NEW_ID
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE), ("", DUP_SEL)]


def test_profile_ready_marker_can_appear_after_ssr_card(monkeypatch):
    duplicate = StubLocator(None, DUP_SEL, count=0, scope=CARD_SEL)
    page = StubPage(
        card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    page._dup_locators[CARD_SEL] = duplicate
    page.ready_count = 0
    page.tick_actions = [
        lambda: (
            setattr(page, "ready_count", 1),
            setattr(duplicate, "_count", 1),
        )
    ]
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert result.success
    assert page.reloads == []
    # Меню открывается ровно одним кликом за попытку — повторный клик на
    # каждом progress-тике на реальном toggle-дропдауне hh.ru закрывал бы уже
    # открытое меню, а не держал его открытым.
    assert page.clicks == [
        (CARD_SEL, RESUME_LIST_ACTION_MORE),
        ("", DUP_SEL),
    ]


def test_hydration_error_reloads_once_then_succeeds(monkeypatch):
    duplicate = StubLocator(None, DUP_SEL, count=0, scope=CARD_SEL)
    page = StubPage(
        card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    page._dup_locators[CARD_SEL] = duplicate
    page.ready_count = 0
    page.goto_action = lambda: page.emit(
        "console", StubConsoleMessage("Error: Minified React error #418")
    )
    page.reload_action = lambda: (
        setattr(page, "ready_count", 1),
        setattr(duplicate, "_count", 1),
    )
    _patch_env(monkeypatch, page)
    monkeypatch.setattr(cr, "PROFILE_STALL_SECONDS", 0.2)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert result.success
    assert page.reloads == ["domcontentloaded"]
    assert page.clicks == [
        (CARD_SEL, RESUME_LIST_ACTION_MORE),
        (CARD_SEL, RESUME_LIST_ACTION_MORE),
        ("", DUP_SEL),
    ]


def test_recovered_hydration_error_does_not_poison_later_failure(monkeypatch):
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=0, wait_ok=False)})
    page.goto_action = lambda: page.emit(
        "console", StubConsoleMessage("Error: Minified React error #418")
    )
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert "квоту прочитать не удалось" in result.reason
    assert "hydration_error" not in result.reason


def test_repeated_hydration_error_fails_without_write(monkeypatch):
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=0)})
    page.ready_count = 0

    def emit_error():
        page.emit("pageerror", RuntimeError("Error: Minified React error #423"))

    page.goto_action = emit_error
    page.reload_action = emit_error
    _patch_env(monkeypatch, page)
    monkeypatch.setattr(cr, "PROFILE_STALL_SECONDS", 0.2)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert result.reason.startswith("hydration_error:")
    assert "react error #423" in result.reason
    assert page.reloads == ["domcontentloaded"]
    assert ("", DUP_SEL) not in page.clicks


def test_profile_request_failure_is_sanitized_and_never_writes(monkeypatch):
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=0)})
    page.ready_count = 0
    failed = StubRequest(
        "https://resume-profile-front.hh.ru/static/remote.js?token=secret#fragment"
    )
    page.goto_action = lambda: page.emit("requestfailed", failed)
    page.reload_action = lambda: page.emit("requestfailed", failed)
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert result.reason.startswith("profile_front_request_failed:")
    assert "https://resume-profile-front.hh.ru/static/remote.js" in result.reason
    assert "secret" not in result.reason
    assert "#fragment" not in result.reason
    assert page.reloads == ["domcontentloaded"]
    assert ("", DUP_SEL) not in page.clicks


def test_profile_http_failure_is_sanitized_and_never_writes(monkeypatch):
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=0)})
    page.ready_count = 0
    failed = StubResponse(
        "https://resume-profile-front.hh.ru/api/bootstrap?token=secret#fragment",
        503,
    )
    page.goto_action = lambda: page.emit("response", failed)
    page.reload_action = lambda: page.emit("response", failed)
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert result.reason.startswith("profile_front_request_failed:")
    assert "https://resume-profile-front.hh.ru/api/bootstrap (HTTP 503)" in result.reason
    assert "secret" not in result.reason
    assert "#fragment" not in result.reason
    assert page.reloads == ["domcontentloaded"]
    assert ("", DUP_SEL) not in page.clicks


def test_unrelated_background_request_does_not_extend_stall(monkeypatch):
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=0)})
    page.ready_count = 0
    background = StubRequest("https://ads.example.test/analytics.js")
    page.tick_actions = [
        lambda: page.emit("requestfinished", background),
        lambda: page.emit("requestfinished", background),
    ]
    _patch_env(monkeypatch, page)
    monkeypatch.setattr(cr, "PROFILE_STALL_SECONDS", 0.2)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert result.reason.startswith("profile_stalled:")
    assert page.reloads == ["domcontentloaded"]
    assert ("", DUP_SEL) not in page.clicks


def test_profile_resource_progress_extends_watchdog_until_ready(monkeypatch):
    duplicate = StubLocator(None, DUP_SEL, count=0, scope=CARD_SEL)
    page = StubPage(
        card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    page._dup_locators[CARD_SEL] = duplicate
    page.ready_count = 0
    profile = StubRequest("https://resume-profile-front.hh.ru/static/chunk.js")
    page.tick_actions = [
        lambda: page.emit("requestfinished", profile),
        lambda: (
            setattr(page, "ready_count", 1),
            setattr(duplicate, "_count", 1),
        ),
    ]
    _patch_env(monkeypatch, page)
    monkeypatch.setattr(cr, "PROFILE_STALL_SECONDS", 0.3)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert result.success
    assert page.reloads == []


def test_absolute_cap_stops_endless_profile_progress(monkeypatch):
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=0)})
    page.ready_count = 0
    profile = StubRequest("https://resume-profile-front.hh.ru/static/chunk.js")
    page.tick_actions = [lambda: page.emit("requestfinished", profile) for _ in range(4)]
    _patch_env(monkeypatch, page)
    monkeypatch.setattr(cr, "PROFILE_STALL_SECONDS", 1.0)
    monkeypatch.setattr(cr, "PROFILE_ABSOLUTE_TIMEOUT_SECONDS", 0.5)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert result.reason.startswith("profile_stalled:")
    assert page.reloads == ["domcontentloaded"]
    assert ("", DUP_SEL) not in page.clicks


def test_duplicate_button_ambiguous_on_page_fails_closed(monkeypatch):
    # Portal дал 2 совпадения на странице — не гадаем, fail-closed.
    page = StubPage(dup_locators={CARD_SEL: StubLocator(None, DUP_SEL, count=2)})
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert "неоднозначно" in result.reason
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE)]


def test_success_via_url_navigation(monkeypatch):
    page = StubPage(
        card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}?query=1",
    )
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert result.success
    assert result.new_resume_id == NEW_ID
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE), ("", DUP_SEL)]


def test_duplicate_portal_is_authorized_by_identity_bound_menu(monkeypatch):
    # Карточка и её menu button identity-bound; открытый portal после этого
    # обязан содержать ровно одно глобальное действие.
    page = StubPage(
        dup_locators={CARD_SEL: StubLocator(None, DUP_SEL)},
        card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert result.success
    assert page.clicks == [(CARD_SEL, RESUME_LIST_ACTION_MORE), ("", DUP_SEL)]


def test_url_shows_old_id_uses_matching_parent_lineage(monkeypatch):
    # Live SPA оставляет URL на исходном/общем wizard route. Серверный
    # parentResumeId identity-bound связывает единственную новую карточку с
    # исходным резюме, поэтому URL больше не обязателен.
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


def test_concurrent_no_navigation_diff_never_succeeds(monkeypatch):
    # url_candidate пуст, но в diff ровно одна новая карточка. Это может быть
    # чужое/конкурентное создание, а не продукт этого клика — success с этим id
    # записал бы в config неверный resume_id. Должен быть uncertain.
    page = StubPage(
        url_after_click=f"https://hh.ru/resume/{OLD_ID}",
        card_hashes=[{OLD_ID}, {OLD_ID, "d" * 38}],
    )
    _patch_env(monkeypatch, page)
    monkeypatch.setattr(
        cr,
        "_resume_lineage",
        lambda p: {
            OLD_ID: cr.ResumeLineage("100", ""),
            "d" * 38: cr.ResumeLineage("102", "999"),
        },
    )
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert result.uncertain is True
    assert result.new_resume_id == "d" * 38
    assert "parentResumeId=999" in result.reason


def test_no_navigation_with_matching_parent_lineage_succeeds(monkeypatch):
    page = StubPage(
        url_after_click="https://hh.ru/profile/resume/professional_role",
        card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID}],
    )
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert result.success
    assert result.new_resume_id == NEW_ID


def test_resume_lineage_reads_server_clone_relation(monkeypatch):
    # SSR-чтение переехало в resume_ids (#891), поэтому шов патчится там.
    monkeypatch.setattr(
        "hhru_bot.resume_ids.parse_initial_state",
        lambda html: {
            "applicantResumes": [
                {
                    "_attributes": {
                        "hash": NEW_ID,
                        "id": 101,
                        "parentResumeId": 100,
                    }
                }
            ]
        },
    )

    lineage = cr._resume_lineage(SimpleNamespace(content=lambda: "<html>"))

    assert lineage == {NEW_ID: cr.ResumeLineage("101", "100")}


def test_resume_lineage_malformed_state_is_unavailable(monkeypatch):
    monkeypatch.setattr("hhru_bot.resume_ids.parse_initial_state", lambda html: ["interstitial"])

    assert cr._resume_lineage(SimpleNamespace(content=lambda: "<html>")) == {}


def test_new_url_without_list_reconciliation_fails_closed(monkeypatch):
    page = StubPage(
        card_hashes=[{OLD_ID}, {OLD_ID}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert result.uncertain is True
    assert "не подтвердил создание копии" in result.reason


def test_url_and_list_reconciliation_mismatch_fails_closed(monkeypatch):
    other_id = "c" * 38
    page = StubPage(
        card_hashes=[{OLD_ID}, {OLD_ID, other_id}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    _patch_env(monkeypatch, page)

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert result.uncertain is True
    assert NEW_ID in result.reason
    assert other_id in result.reason


def test_no_navigation_and_no_new_card_fails(monkeypatch):
    # POST не прошёл: URL не изменился, в списке ничего нового — fail-closed.
    page = StubPage(card_hashes=[{OLD_ID}, {OLD_ID}])
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert result.uncertain is True
    assert result.new_resume_id == ""


def test_multiple_new_cards_ambiguous_fails(monkeypatch):
    # Diff дал два новых хэша — определить копию нельзя, fail-closed.
    page = StubPage(card_hashes=[{OLD_ID}, {OLD_ID, NEW_ID, "c" * 38}])
    _patch_env(monkeypatch, page)
    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)
    assert not result.success
    assert result.uncertain is True


def test_reconcile_exception_after_write_is_uncertain(monkeypatch):
    # WRITE-клик уже отправлен, но reconcile падает (не прочитан список) — это
    # не доказывает, что копия не создана, поэтому исход uncertain (fail-closed,
    # зеркалит #176/#207).
    page = StubPage(
        card_hashes=[{OLD_ID}],
        url_after_click=f"https://hh.ru/resume/{NEW_ID}",
    )
    _patch_env(monkeypatch, page)

    def boom():
        raise cr.ResumeListIndeterminate("список не прочитан")

    monkeypatch.setattr(cr, "_wait_resume_list_ready", lambda p: boom())

    result = cr.copy_resume_on_hh(page, _resume(), dry_run=False)

    assert not result.success
    assert result.uncertain is True
    assert "не подтверждено" in result.reason

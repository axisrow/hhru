"""Тесты навигации hh.ru: потолок timeout + retry goto (#80).

Без браузера — через MagicMock. Страхуют регрессию «забыли timeout»: hh.ru под
DDoS-Guard грузится 33с+ против дефолта Playwright 30с, и goto падает. Решение
#80 — context-wide set_default_navigation_timeout(GOTO_TIMEOUT_MS) единым
источником + helper goto_hh с retry (DDoS-Guard часто пропускает со 2-й попытки).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot import browser
from hhru_bot.browser import (
    GOTO_TIMEOUT_MS,
    NotAuthenticated,
    ThrottledChannelDetected,
    goto_hh,
    open_confirmed_resume,
    require_authenticated_page,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("headless", [True, False])
def test_sandbox_failure_is_classified_for_headless_and_headed(headless):
    """Chromium sandbox diagnostics must keep the CLI output structured."""
    playwright = MagicMock(name="Playwright")
    playwright.chromium.launch.side_effect = PlaywrightError(
        "FATAL:base/apple/mach_port_rendezvous_mac.cc:159 "
        "Check failed: kr == KERN_SUCCESS. "
        "bootstrap_check_in org.chromium.Chromium.MachPortRendezvousServer.13682: "
        "Permission denied (1100)"
    )

    with pytest.raises(browser.BrowserLaunchError) as excinfo:
        browser.launch_browser(playwright, headless=headless)

    message = str(excinfo.value)
    assert "CODEX_SANDBOX_BROWSER_FAILURE" in message
    assert "вне sandbox" in message
    assert "headed/headless" in message


def test_non_sandbox_launch_failure_is_not_reclassified():
    playwright = MagicMock(name="Playwright")
    playwright.chromium.launch.side_effect = PlaywrightError("browser executable missing")

    with pytest.raises(PlaywrightError, match="browser executable missing"):
        browser.launch_browser(playwright, headless=True)


def test_generic_permission_denied_launch_failure_is_not_reclassified():
    playwright = MagicMock(name="Playwright")
    playwright.chromium.launch.side_effect = PlaywrightError(
        "Permission denied opening browser profile"
    )

    with pytest.raises(PlaywrightError, match="Permission denied opening"):
        browser.launch_browser(playwright, headless=True)


def test_goto_timeout_constant_is_90_seconds():
    """Эталон из исследования референсов #80: 90с под DDoS-Guard (Steev193)."""
    assert GOTO_TIMEOUT_MS == 90_000


def _fake_playwright(monkeypatch, *, calls):
    """Мок sync_playwright: возвращает контекст, записывающий set_default_*.

    sync_playwright() — context manager, yield'ит объект с .chromium.launch().
    """

    @contextmanager
    def _sync_playwright():
        context = MagicMock(name="BrowserContext")
        calls["set_default_navigation_timeout"] = context.set_default_navigation_timeout
        playwright = MagicMock(name="Playwright")
        playwright.chromium.launch.return_value.new_context.return_value = context
        yield playwright

    monkeypatch.setattr(browser, "sync_playwright", _sync_playwright)


def test_launch_context_sets_default_navigation_timeout(monkeypatch, tmp_path):
    """#80 минимум: после new_context — set_default_navigation_timeout(GOTO_TIMEOUT_MS).

    Покрывает ВСЕ goto/wait_for_url единым источником (включая двухшаговую
    навигацию формы отклика, CLAUDE.md п.4, #179) — без явного timeout в каждом вызове.
    """
    calls: dict = {}
    _fake_playwright(monkeypatch, calls=calls)

    with browser.launch_context(tmp_path / "session.json", headless=True):
        pass

    calls["set_default_navigation_timeout"].assert_called_once_with(GOTO_TIMEOUT_MS)


def test_launch_context_sets_timeout_even_without_session(monkeypatch, tmp_path):
    """Тот же потолок навигации при отсутствии файла сессии (холодный запуск)."""
    calls: dict = {}
    _fake_playwright(monkeypatch, calls=calls)
    missing = tmp_path / "does_not_exist.json"

    with browser.launch_context(missing, headless=True):
        pass

    calls["set_default_navigation_timeout"].assert_called_once_with(GOTO_TIMEOUT_MS)


def test_launch_context_none_never_loads_storage_state(monkeypatch):
    captured: dict = {}

    @contextmanager
    def _sync_playwright():
        context = MagicMock(name="BrowserContext")
        fake_browser = MagicMock(name="Browser")

        def new_context(**kwargs):
            captured.update(kwargs)
            return context

        fake_browser.new_context.side_effect = new_context
        playwright = MagicMock(name="Playwright")
        playwright.chromium.launch.return_value = fake_browser
        yield playwright

    monkeypatch.setattr(browser, "sync_playwright", _sync_playwright)

    with browser.launch_context(None, headless=True):
        pass

    assert "storage_state" not in captured


def test_launch_context_suppresses_only_target_closed_cleanup(monkeypatch, tmp_path):
    calls: dict = {}

    # Obtain the actual fakes by replacing the helper with explicit resources.
    @contextmanager
    def _sync_playwright():
        fake_context = MagicMock(name="BrowserContext")
        fake_context.close.side_effect = PlaywrightError(
            "Target page, context or browser has been closed"
        )
        fake_browser = MagicMock(name="Browser")
        fake_browser.new_context.return_value = fake_context
        playwright = MagicMock(name="Playwright")
        playwright.chromium.launch.return_value = fake_browser
        calls["browser"] = fake_browser
        yield playwright

    monkeypatch.setattr(browser, "sync_playwright", _sync_playwright)
    with browser.launch_context(tmp_path / "session.json", headless=True):
        pass
    calls["browser"].close.assert_called_once()


def test_launch_context_suppresses_driver_closed_cleanup(monkeypatch, tmp_path):
    @contextmanager
    def _sync_playwright():
        fake_context = MagicMock(name="BrowserContext")
        fake_context.close.side_effect = Exception(
            "Browser.close: Connection closed while reading from the driver"
        )
        fake_browser = MagicMock(name="Browser")
        fake_browser.new_context.return_value = fake_context
        playwright = MagicMock(name="Playwright")
        playwright.chromium.launch.return_value = fake_browser
        yield playwright

    monkeypatch.setattr(browser, "sync_playwright", _sync_playwright)
    with browser.launch_context(tmp_path / "session.json", headless=True):
        pass


def test_launch_context_propagates_unrelated_cleanup_error(monkeypatch, tmp_path):
    @contextmanager
    def _sync_playwright():
        fake_context = MagicMock(name="BrowserContext")
        fake_context.close.side_effect = PlaywrightError("transport corruption")
        fake_browser = MagicMock(name="Browser")
        fake_browser.new_context.return_value = fake_context
        playwright = MagicMock(name="Playwright")
        playwright.chromium.launch.return_value = fake_browser
        yield playwright

    monkeypatch.setattr(browser, "sync_playwright", _sync_playwright)
    with pytest.raises(PlaywrightError, match="transport corruption"):
        with browser.launch_context(tmp_path / "session.json", headless=True):
            pass


def test_launch_context_propagates_unrelated_generic_cleanup_error(monkeypatch, tmp_path):
    @contextmanager
    def _sync_playwright():
        fake_context = MagicMock(name="BrowserContext")
        fake_context.close.side_effect = Exception("generic cleanup corruption")
        fake_browser = MagicMock(name="Browser")
        fake_browser.new_context.return_value = fake_context
        playwright = MagicMock(name="Playwright")
        playwright.chromium.launch.return_value = fake_browser
        yield playwright

    monkeypatch.setattr(browser, "sync_playwright", _sync_playwright)
    with pytest.raises(Exception, match="generic cleanup corruption"):
        with browser.launch_context(tmp_path / "session.json", headless=True):
            pass


def _page_gotos(*, results):
    """Мок Page: page.goto выбрасывает исключения из results по порядку.

    results — список: либо None (goto успешен), либо Exception/класс исключения.
    Возвращает (page, gotos), где gotos — список переданных url.
    """
    gotos: list[str] = []
    iterator = iter(results)

    def _goto(url, **kwargs):
        gotos.append(url)
        outcome = next(iterator)
        if outcome is None:
            return None
        raise outcome() if isinstance(outcome, type) else outcome

    page = MagicMock(name="Page")
    page.goto.side_effect = _goto
    return page, gotos


def test_goto_hh_retries_on_timeout_then_succeeds(monkeypatch):
    """DDoS-Guard пропускает со 2-й попытки: 1-я PlaywrightTimeoutError, 2-я успех."""
    monkeypatch.setattr(browser.time, "sleep", lambda _: None)
    page, gotos = _page_gotos(results=[PlaywrightTimeoutError("30s exceeded"), None])

    goto_hh(page, "https://hh.ru/search/vacancy")

    assert gotos == ["https://hh.ru/search/vacancy"] * 2


def test_goto_hh_raises_after_max_attempts(monkeypatch):
    """Все попытки провалены — последняя ошибка пробрасывается (как обычный goto)."""
    monkeypatch.setattr(browser.time, "sleep", lambda _: None)
    page, gotos = _page_gotos(results=[PlaywrightTimeoutError("slow")] * 3)

    with pytest.raises(PlaywrightTimeoutError):
        goto_hh(page, "https://hh.ru/search/vacancy")

    assert len(gotos) == 3  # _GOTO_MAX_ATTEMPTS


def test_goto_hh_no_retry_on_success(monkeypatch):
    """Удачный goto с первой попытки — ровно один вызов, без sleep."""
    sleeps: list = []
    monkeypatch.setattr(browser.time, "sleep", lambda s: sleeps.append(s))
    page, gotos = _page_gotos(results=[None])

    goto_hh(page, "https://hh.ru/search/vacancy")

    assert gotos == ["https://hh.ru/search/vacancy"]
    assert sleeps == []


def test_goto_hh_uses_domcontentloaded(monkeypatch):
    """Рекомендация референсов #80: domcontentloaded, НЕ networkidle (DDoS-Guard).

    Гарантируем, что goto_hh не переключился обратно на networkidle/load.
    """
    monkeypatch.setattr(browser.time, "sleep", lambda _: None)
    page = MagicMock(name="Page")
    page.goto.return_value = None

    goto_hh(page, "https://hh.ru/search/vacancy")

    page.goto.assert_called_once_with("https://hh.ru/search/vacancy", wait_until="domcontentloaded")


# --- has_auth_cookie (Codex review, #135) ------------------------------------


def test_has_auth_cookie_true_when_hhtoken_present():
    page = MagicMock(name="Page")
    page.context.cookies.return_value = [
        {"name": "unrelated", "value": "x"},
        {"name": "hhtoken", "value": "abc"},
    ]

    assert browser.has_auth_cookie(page) is True


def test_has_auth_cookie_false_when_hhtoken_absent():
    page = MagicMock(name="Page")
    page.context.cookies.return_value = [{"name": "unrelated", "value": "x"}]

    assert browser.has_auth_cookie(page) is False


def test_has_auth_cookie_false_on_empty_cookies():
    page = MagicMock(name="Page")
    page.context.cookies.return_value = []

    assert browser.has_auth_cookie(page) is False


def test_require_authenticated_page_rejects_missing_cookie():
    page = MagicMock(name="Page")
    page.context.cookies.return_value = []
    with pytest.raises(NotAuthenticated, match="hhtoken"):
        require_authenticated_page(page)


def test_require_authenticated_page_rejects_login_form_with_cookie(monkeypatch):
    page = MagicMock(name="Page")
    page.context.cookies.return_value = [{"name": "hhtoken"}]
    monkeypatch.setattr(browser, "has_login_form", lambda page: True)
    with pytest.raises(NotAuthenticated, match="форму входа"):
        require_authenticated_page(page)


def test_open_confirmed_resume_checks_auth_and_identity(monkeypatch):
    page = MagicMock(name="Page")
    page.url = "https://hh.ru/resume/123"
    page.context.cookies.return_value = [{"name": "hhtoken"}]
    monkeypatch.setattr(browser, "goto_hh", lambda page, url: None)
    monkeypatch.setattr(browser, "has_login_form", lambda page: False)
    open_confirmed_resume(page, "123")

    page.url = "https://hh.ru/resume/other"
    with pytest.raises(ValueError, match="identity"):
        open_confirmed_resume(page, "123")


def test_browser_has_no_legacy_url_auth_checker():
    assert not hasattr(browser, "is_logged_in")


def test_goto_hh_waits_for_ready_selector(monkeypatch):
    """#142: ready_selector — после удачного goto ждём data-qa маркер страницы.

    Параметр существовал с #80, но ни один caller его не передавал (мёртвый).
    Гарантируем, что goto_hh(url, ready_selector=sel) вызывает
    page.locator(sel).wait_for(timeout=GOTO_TIMEOUT_MS) после goto.
    """
    monkeypatch.setattr(browser.time, "sleep", lambda _: None)
    page = MagicMock(name="Page")
    page.goto.return_value = None

    goto_hh(page, "https://hh.ru/applicant/resumes", ready_selector="[data-qa='resume']")

    page.locator.assert_called_once_with("[data-qa='resume']")
    page.locator.return_value.wait_for.assert_called_once_with(timeout=GOTO_TIMEOUT_MS)


def test_goto_hh_ready_selector_absent_retries_then_raises(monkeypatch):
    """#142: ready_selector не появился → wait_for кидает TimeoutError → retry+raise.

    goto_hh должен трактовать таймаут ready_selector так же, как таймаут самого
    goto: retry до _GOTO_MAX_ATTEMPTS, на последней — проброс. Иначе caller,
    передавший ready_selector, получил бы «успешный» goto на непрогрузившейся
    странице.
    """
    monkeypatch.setattr(browser.time, "sleep", lambda _: None)
    page = MagicMock(name="Page")
    page.goto.return_value = None
    # ready_selector не появляется ни на одной попытке
    page.locator.return_value.wait_for.side_effect = PlaywrightTimeoutError("no marker")

    with pytest.raises(PlaywrightTimeoutError):
        goto_hh(page, "https://hh.ru/applicant/resumes", ready_selector="[data-qa='resume']")

    # retry сработал: 3 goto-попытки (ready_selector проверяется после каждой)
    assert page.goto.call_count == 3


def _page_with_response_listener(monkeypatch):
    """Мок Page, эмулирующий page.on('response', handler)/remove_listener.

    goto_hh регистрирует ровно один response-listener за попытку; тест
    вызывает его вручную (эмулируя приход HTTP-ответа сервера ДО таймаута),
    затем поднимает page.goto из PlaywrightTimeoutError, как реальный
    Playwright делает при таймауте после уже полученных заголовков.
    """
    monkeypatch.setattr(browser.time, "sleep", lambda _: None)
    listeners: list = []
    page = MagicMock(name="Page")
    page.on.side_effect = lambda _event, handler: listeners.append(handler)
    return page, listeners


def _fake_response(url: str, status: int):
    response = MagicMock(name="Response")
    response.url = url
    response.status = status
    return response


def test_goto_hh_wraps_timeout_as_throttled_when_response_observed(monkeypatch):
    """#749: сервер ответил (status 200 на навигационный URL), но тело не
    успело докачаться — PlaywrightTimeoutError без net::ERR_*.  goto_hh
    должен переквалифицировать это в ThrottledChannelDetected — throttled-
    канал, а не анти-бот/дрейф селектора.
    """
    url = "https://hh.ru/search/vacancy"
    page, listeners = _page_with_response_listener(monkeypatch)

    def _goto(_url, **_kwargs):
        # Сервер успел ответить до того, как истёк таймаут рендера.
        for handler in listeners:
            handler(_fake_response(url, 200))
        raise PlaywrightTimeoutError("Page.goto: Timeout 90000ms exceeded.")

    page.goto.side_effect = _goto

    with pytest.raises(ThrottledChannelDetected):
        goto_hh(page, url)

    assert page.goto.call_count == 3  # ретраится и на последней пробрасывает


def test_goto_hh_no_response_stays_unclassified_timeout(monkeypatch):
    """Симметричный случай: response вообще не пришёл (анти-бот/дрейф
    селектора/сеть недоступна) — ошибка НЕ должна переквалифицироваться.
    """
    page, _listeners = _page_with_response_listener(monkeypatch)
    page.goto.side_effect = PlaywrightTimeoutError("Page.goto: Timeout 90000ms exceeded.")

    with pytest.raises(PlaywrightTimeoutError) as excinfo:
        goto_hh(page, "https://hh.ru/search/vacancy")

    assert not isinstance(excinfo.value, ThrottledChannelDetected)


def test_goto_hh_net_err_not_reclassified_as_throttled(monkeypatch):
    """net::ERR_* остаётся в своём классе (#748) даже если listener успел
    увидеть какой-то response до обрыва соединения — net::ERR_* в тексте
    ошибки приоритетнее throttled-эвристики.
    """
    url = "https://hh.ru/search/vacancy"
    page, listeners = _page_with_response_listener(monkeypatch)

    def _goto(_url, **_kwargs):
        for handler in listeners:
            handler(_fake_response(url, 200))
        raise PlaywrightError("Page.goto: net::ERR_CONNECTION_RESET")

    page.goto.side_effect = _goto

    with pytest.raises(PlaywrightError) as excinfo:
        goto_hh(page, url)

    assert not isinstance(excinfo.value, ThrottledChannelDetected)
    assert "net::ERR_" in str(excinfo.value)


def test_goto_hh_response_for_unrelated_url_ignored(monkeypatch):
    """Response для другого URL (напр. аналитика/трекер) не должен
    засчитываться как подтверждение навигационного запроса.
    """
    page, listeners = _page_with_response_listener(monkeypatch)

    def _goto(_url, **_kwargs):
        for handler in listeners:
            handler(_fake_response("https://hh.ru/analytics/beacon", 200))
        raise PlaywrightTimeoutError("Page.goto: Timeout 90000ms exceeded.")

    page.goto.side_effect = _goto

    with pytest.raises(PlaywrightTimeoutError) as excinfo:
        goto_hh(page, "https://hh.ru/search/vacancy")

    assert not isinstance(excinfo.value, ThrottledChannelDetected)


def test_goto_hh_removes_response_listener_after_each_attempt(monkeypatch):
    """Listener не должен копиться между попытками/вызовами (утечка памяти)."""
    page, listeners = _page_with_response_listener(monkeypatch)
    removed: list = []
    page.remove_listener.side_effect = lambda _event, handler: removed.append(handler)
    page.goto.return_value = None

    goto_hh(page, "https://hh.ru/search/vacancy")

    assert len(listeners) == 1
    assert removed == listeners

"""Тесты навигации hh.ru: потолок timeout + retry goto (#80).

Без браузера — через MagicMock. Страхуют регрессию «забыли timeout»: hh.ru под
DDoS-Guard грузится 33с+ против дефолта Playwright 30с, и goto падает. Решение
#80 — context-wide set_default_navigation_timeout(GOTO_TIMEOUT_MS) единым
источником + helper goto_hh с retry (DDoS-Guard часто пропускает со 2-й попытки).
"""

from __future__ import annotations

import re
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
    open_hydrated_resume_editor,
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
    # #972: баннер-детектор (locator→filter→count) на живой странице — int;
    # без этой настройки MagicMock-цепочка вернула бы MagicMock, упав в `> 0`.
    page.locator.return_value.filter.return_value.count.return_value = 0
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


def _fake_response(url: str, status: int, *, navigation: bool = True):
    """Мок Response с настраиваемым структурным признаком навигации.

    navigation=False эмулирует SPA-субресурс (xhr/fetch/prefetch) — code-review
    round 1: совпадение URL само по себе не доказывает, что это ответ на
    document-запрос, а не побочный запрос по тому же пути.
    """
    response = MagicMock(name="Response")
    response.url = url
    response.status = status
    response.request.is_navigation_request.return_value = navigation
    response.request.resource_type = "document" if navigation else "xhr"
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


def test_goto_hh_non_timeout_error_not_reclassified_as_throttled(monkeypatch):
    """Cycle-review PR #760, Codex finding: throttled-классификация обёрнута
    вокруг ЛЮБОГО ``PlaywrightError`` без ``net::ERR_*``, а не только вокруг
    ``PlaywrightTimeoutError``, хотя ``ThrottledChannelDetected`` документирован
    именно как "timed out AFTER" (docstring, browser.py). Не-timeout ошибка
    (напр. "Navigation interrupted by another navigation" — конкурирующая
    JS-навигация, а не throttling) с наблюдённым response не должна
    переквалифицироваться в throttled: это меняет смысл диагностики на
    неверный без всякого основания.
    """
    url = "https://hh.ru/search/vacancy"
    page, listeners = _page_with_response_listener(monkeypatch)

    def _goto(_url, **_kwargs):
        for handler in listeners:
            handler(_fake_response(url, 200))
        raise PlaywrightError("Navigation interrupted by another navigation")

    page.goto.side_effect = _goto

    with pytest.raises(PlaywrightError) as excinfo:
        goto_hh(page, url)

    assert not isinstance(excinfo.value, ThrottledChannelDetected)


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


def test_goto_hh_non_navigation_response_same_path_ignored(monkeypatch):
    """Code-review round 1, finding #2: субресурс (xhr/fetch) по ТОМУ ЖЕ
    пути — не доказательство того, что сервер отдал документ. Навигационность
    берётся из структурного признака Playwright, а не из совпадения URL.
    """
    url = "https://hh.ru/search/vacancy"
    page, listeners = _page_with_response_listener(monkeypatch)

    def _goto(_url, **_kwargs):
        for handler in listeners:
            handler(_fake_response(url, 200, navigation=False))
        raise PlaywrightTimeoutError("Page.goto: Timeout 90000ms exceeded.")

    page.goto.side_effect = _goto

    with pytest.raises(PlaywrightTimeoutError) as excinfo:
        goto_hh(page, url)

    assert not isinstance(excinfo.value, ThrottledChannelDetected)


def test_goto_hh_ready_selector_timeout_not_throttled(monkeypatch):
    """Code-review round 1, finding #1 (блокер): goto прошёл (канал доказано
    живой, ответ 200 наблюдён), но ready_selector не появился — дрейф
    селектора/анти-бот интерстишл на уже докачанной странице. Причина
    остаётся неопределённой (#748) — это НЕ throttled-канал.
    """
    url = "https://hh.ru/resume/abc"
    page, listeners = _page_with_response_listener(monkeypatch)

    def _goto(_url, **_kwargs):
        for handler in listeners:
            handler(_fake_response(url, 200))
        return None  # goto успешен — канал доказано живой

    page.goto.side_effect = _goto
    page.locator.return_value.wait_for.side_effect = PlaywrightTimeoutError(
        "Locator.wait_for: Timeout 90000ms exceeded."
    )

    with pytest.raises(PlaywrightTimeoutError) as excinfo:
        goto_hh(page, url, ready_selector="[data-qa='resume-update-button']")

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


def test_open_hydrated_resume_editor_accepts_matching_path_and_query(monkeypatch):
    page = MagicMock(name="Page")
    page.url = "https://hh.ru/profile/edit/primaryEducation?resumeFrom=abc123&hhtmFrom=resume"
    editor = MagicMock(name="editor")
    editor.count.return_value = 1
    page.locator.return_value = editor

    result = open_hydrated_resume_editor(
        page,
        trigger_selector="[data-qa='x']",
        editor_selector="[data-qa='y']",
        profile_path="/resume/abc123",
        edit_path=re.compile(r"/profile/edit/primaryEducation(?=[?#]|$)"),
        expected_query={"resumeFrom": "abc123"},
        click_trigger=False,
    )

    assert result is editor


def test_open_hydrated_resume_editor_rejects_mismatched_query(monkeypatch):
    page = MagicMock(name="Page")
    page.url = "https://hh.ru/profile/edit/primaryEducation?resumeFrom=wrong&hhtmFrom=resume"
    editor = MagicMock(name="editor")
    editor.count.return_value = 1
    page.locator.return_value = editor

    with pytest.raises(RuntimeError, match="открыта не для того резюме"):
        open_hydrated_resume_editor(
            page,
            trigger_selector="[data-qa='x']",
            editor_selector="[data-qa='y']",
            profile_path="/resume/abc123",
            edit_path=re.compile(r"/profile/edit/primaryEducation(?=[?#]|$)"),
            expected_query={"resumeFrom": "abc123"},
            click_trigger=False,
        )


def test_open_hydrated_resume_editor_rejects_mismatched_path_with_query(monkeypatch):
    page = MagicMock(name="Page")
    page.url = "https://hh.ru/profile/edit/additionalEducation?resumeFrom=abc123&hhtmFrom=resume"
    editor = MagicMock(name="editor")
    editor.count.return_value = 1
    page.locator.return_value = editor

    with pytest.raises(RuntimeError, match="открыта не для того резюме"):
        open_hydrated_resume_editor(
            page,
            trigger_selector="[data-qa='x']",
            editor_selector="[data-qa='y']",
            profile_path="/resume/abc123",
            edit_path=re.compile(r"/profile/edit/primaryEducation(?=[?#]|$)"),
            expected_query={"resumeFrom": "abc123"},
            click_trigger=False,
        )


def test_open_hydrated_resume_editor_omits_query_check_when_not_expected(monkeypatch):
    page = MagicMock(name="Page")
    page.url = "https://hh.ru/resume/edit/resume-id/keySkills?foo=bar"
    editor = MagicMock(name="editor")
    editor.count.return_value = 1
    page.locator.return_value = editor

    result = open_hydrated_resume_editor(
        page,
        trigger_selector="[data-qa='x']",
        editor_selector="[data-qa='y']",
        profile_path="/resume/resume-id",
        edit_path="/resume/edit/resume-id/keySkills",
        click_trigger=False,
    )

    assert result is editor


def test_labelled_field_requires_one_exact_match() -> None:
    """#773: Magritte forms drop data-qa from some inputs, leaving the visible
    label as the only handle. Exactness matters — a partial match would address
    a different field, and on a write form that means writing to the wrong
    control."""
    page = MagicMock()
    field = MagicMock()
    field.count.return_value = 1
    page.get_by_label.return_value = field

    assert browser.labelled_field(page, "Название") is field
    page.get_by_label.assert_called_once_with("Название", exact=True)


@pytest.mark.parametrize("count", [0, 2])
def test_labelled_field_fails_closed_on_ambiguity(count: int) -> None:
    """Zero or several matches must stop the caller the same way an
    unconfirmed data-qa does, never silently pick one."""
    page = MagicMock()
    field = MagicMock()
    field.count.return_value = count
    page.get_by_label.return_value = field

    with pytest.raises(browser.PageStateIndeterminate):
        browser.labelled_field(page, "Название")


# --- #1002: census отрисованных контролов + census-компаньон дампов ----------


def test_rendered_controls_census_returns_rows_from_page(monkeypatch):
    """census возвращает строки, отданные page.evaluate, без переработки."""
    from hhru_bot.browser import rendered_controls_census

    page = MagicMock()
    page.evaluate.return_value = [
        {"qa": "resume-profile-common-name-input", "tag": "input", "role": "", "label": "", "text": "", "visible": True},
        {"qa": "", "tag": "div", "role": "", "label": "Город", "text": "Город", "visible": False},
    ]
    rows = rendered_controls_census(page)
    assert rows == page.evaluate.return_value
    page.evaluate.assert_called_once()


def test_census_table_renders_visibility_column():
    from hhru_bot.browser import census_table

    table = census_table(
        [
            {"qa": "x-input", "tag": "input", "role": "", "label": "", "text": "Тестов", "visible": True},
            {"qa": "", "tag": "div", "role": "", "label": "Город", "text": "Город", "visible": False},
        ]
    )
    assert "да" in table and "нет" in table
    assert "Городская" not in table  # литералы бандлов не попадают по построению


def test_dump_page_html_writes_census_companion(monkeypatch, tmp_path):
    """#1002: к каждому дампу пишется census-компаньон; сбой census не
    ломает дамп."""
    from hhru_bot import browser

    page = MagicMock()
    page.content.return_value = "<html><body>дамп</body></html>"
    page.evaluate.return_value = [
        {"qa": "name-input", "tag": "input", "role": "", "label": "", "text": "Тест", "visible": True},
    ]
    monkeypatch.setattr(browser, "LOG_DIR", tmp_path) if hasattr(browser, "LOG_DIR") else None
    dump = browser.dump_page_html(page, "census_companion_check")
    assert dump is not None and dump.exists()
    companion = dump.with_suffix(".census.txt")
    assert companion.exists()
    assert "name-input" in companion.read_text(encoding="utf-8")


def test_dump_page_html_survives_census_failure(monkeypatch, tmp_path):
    from hhru_bot import browser

    page = MagicMock()
    page.content.return_value = "<html></html>"
    page.evaluate.side_effect = RuntimeError("census crashed")
    dump = browser.dump_page_html(page, "census_failure_check")
    assert dump is not None and dump.exists()

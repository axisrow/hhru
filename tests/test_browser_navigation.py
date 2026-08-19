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

    with pytest.raises(browser.BrowserLaunchError, match="CODEX_SANDBOX_BROWSER_FAILURE"):
        browser.launch_browser(playwright, headless=headless)


def test_non_sandbox_launch_failure_is_not_reclassified():
    playwright = MagicMock(name="Playwright")
    playwright.chromium.launch.side_effect = PlaywrightError("browser executable missing")

    with pytest.raises(PlaywrightError, match="browser executable missing"):
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

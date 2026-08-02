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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hhru_bot import browser
from hhru_bot.browser import GOTO_TIMEOUT_MS, goto_hh


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

    Покрывает ВСЕ goto/expect_navigation единым источником (включая двухшаговую
    навигацию формы отклика, CLAUDE.md п.4) — без явного timeout в каждом вызове.
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

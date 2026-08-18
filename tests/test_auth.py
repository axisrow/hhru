from __future__ import annotations

import itertools
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hhru_bot import auth

pytestmark = pytest.mark.integration


def _make_playwright(monkeypatch):
    """Собирает моки sync_playwright()/browser/context/page для login(); возвращает context."""
    context = MagicMock()
    context.new_page.return_value = MagicMock()
    browser = MagicMock()
    browser.new_context.return_value = context

    playwright = MagicMock()
    playwright.chromium.launch.return_value = browser
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    manager.__exit__.return_value = None
    monkeypatch.setattr(auth, "sync_playwright", lambda: manager)
    monkeypatch.setattr(auth, "goto_hh", MagicMock())
    monkeypatch.setattr(time, "sleep", lambda _: None)
    return context


def test_login_does_not_save_session_without_hhtoken(monkeypatch, tmp_path):
    context = _make_playwright(monkeypatch)
    monkeypatch.setattr(auth, "has_auth_cookie", lambda p: False)
    monkeypatch.setattr(auth, "has_login_form", lambda p: True)
    monkeypatch.setattr(time, "monotonic", iter([0, 301, 301]).__next__)

    config = MagicMock(storage_state_file=tmp_path / "session.json", user_agent=None)

    with pytest.raises(RuntimeError, match="hhtoken"):
        auth.login(config)

    context.storage_state.assert_not_called()


def test_login_succeeds_when_auth_confirmed_on_third_poll(monkeypatch, tmp_path):
    """Вход появился на 3-й итерации опроса -> storage_state сохранён, без исключений."""
    context = _make_playwright(monkeypatch)
    # Первые 2 опроса — форма ещё видна/куки нет, на 3-й — has_auth_cookie=True и
    # has_login_form=False (позитивный маркер входа).
    auth_cookie_results = itertools.chain([False, False, True], itertools.repeat(True))
    login_form_results = itertools.chain([True, True, False], itertools.repeat(False))
    monkeypatch.setattr(auth, "has_auth_cookie", lambda p: next(auth_cookie_results))
    monkeypatch.setattr(auth, "has_login_form", lambda p: next(login_form_results))
    monkeypatch.setattr(time, "monotonic", itertools.chain([0, 2, 4], itertools.repeat(4)).__next__)

    config = MagicMock(storage_state_file=tmp_path / "session.json", user_agent=None)
    read_profile = MagicMock()
    monkeypatch.setattr(auth, "read_account_profile", read_profile)

    auth.login(config)

    context.storage_state.assert_called_once_with(path=str(config.storage_state_file))
    read_profile.assert_called_once_with(context.new_page.return_value, Path("data/history.db"))


def test_login_times_out_when_login_form_never_disappears(monkeypatch, tmp_path):
    """Кука есть, но форма входа осталась (отозванная сессия) -> НЕ успех, RuntimeError."""
    context = _make_playwright(monkeypatch)
    monkeypatch.setattr(auth, "has_auth_cookie", lambda p: True)
    monkeypatch.setattr(auth, "has_login_form", lambda p: True)
    monkeypatch.setattr(time, "monotonic", iter([0, 301, 301]).__next__)

    config = MagicMock(storage_state_file=tmp_path / "session.json", user_agent=None)

    with pytest.raises(RuntimeError, match="не завершён"):
        auth.login(config)

    context.storage_state.assert_not_called()


def test_login_never_calls_input_in_non_tty(monkeypatch, tmp_path):
    """Охранный тест против регресса #164: login() не должен звать input() вообще."""
    context = _make_playwright(monkeypatch)
    monkeypatch.setattr(auth, "has_auth_cookie", lambda p: True)
    monkeypatch.setattr(auth, "has_login_form", lambda p: False)
    monkeypatch.setattr(time, "monotonic", iter([0, 0]).__next__)

    config = MagicMock(storage_state_file=tmp_path / "session.json", user_agent=None)

    with patch("builtins.input", side_effect=EOFError("EOF when reading a line")) as mocked_input:
        auth.login(config)

    mocked_input.assert_not_called()
    context.storage_state.assert_called_once_with(path=str(config.storage_state_file))

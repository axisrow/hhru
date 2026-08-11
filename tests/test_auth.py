from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hhru_bot import auth


def test_login_does_not_save_session_without_hhtoken(monkeypatch, tmp_path):
    context = MagicMock()
    page = MagicMock()
    context.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.return_value = context

    playwright = MagicMock()
    playwright.chromium.launch.return_value = browser
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    manager.__exit__.return_value = None
    monkeypatch.setattr(auth, "sync_playwright", lambda: manager)
    monkeypatch.setattr(auth, "goto_hh", MagicMock())
    monkeypatch.setattr("builtins.input", lambda _: "")
    context.cookies.return_value = [{"name": "other", "value": "abc"}]

    config = MagicMock(storage_state_file=tmp_path / "session.json", user_agent=None)

    with pytest.raises(RuntimeError, match="hhtoken"):
        auth.login(config)

    context.storage_state.assert_not_called()

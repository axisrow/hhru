from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from hhru_bot.commands import refresh_token

pytestmark = pytest.mark.integration


class Context:
    def __init__(self, page):
        self.page = page
        self.saved = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def new_page(self):
        return self.page

    def storage_state(self, *, path):
        self.saved.append(path)


def _args(force=False, headless=False):
    return argparse.Namespace(config="config.yaml", force=force, headless=headless)


def _patch_browser(monkeypatch, context, *, auth=True, login_form=False):
    events = []
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **kw: context)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda page, url: events.append(("goto", url)))
    monkeypatch.setattr(
        "hhru_bot.browser.has_auth_cookie", lambda page: events.append("cookie") or auth
    )
    monkeypatch.setattr(
        "hhru_bot.browser.has_login_form", lambda page: events.append("login") or login_form
    )
    return events


def test_registers_refresh_token_and_force():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    refresh_token.register(subparsers)

    args = parser.parse_args(["refresh-token", "--force"])
    assert args.force is True
    assert args.func is refresh_token.run


def test_check_valid_session_does_not_resave(monkeypatch, capsys):
    context = Context(object())
    events = _patch_browser(monkeypatch, context)
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: SimpleNamespace(storage_state_file=Path("data/session.json"), user_agent="UA"),
    )

    assert refresh_token.run(_args()) is False
    assert "обновление не требуется" in capsys.readouterr().out
    assert context.saved == []
    assert events == [("goto", refresh_token.SESSION_CHECK_URL), "cookie", "login"]


def test_force_resaves_only_after_authenticated_page(monkeypatch, capsys, tmp_path):
    context = Context(object())
    _patch_browser(monkeypatch, context)
    destination = tmp_path / "storage_state" / "session.json"
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: SimpleNamespace(storage_state_file=destination, user_agent=None),
    )

    assert refresh_token.run(_args(force=True)) is False
    assert context.saved == [str(destination)]
    assert "Сессия пересохранена" in capsys.readouterr().out


def test_invalid_session_is_fail_closed_and_not_saved(monkeypatch, capsys):
    context = Context(object())
    _patch_browser(monkeypatch, context, auth=False)
    destination = Path("data/session.json")
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: SimpleNamespace(storage_state_file=destination, user_agent=None),
    )

    assert refresh_token.run(_args(force=True)) is True
    assert context.saved == []
    assert "[FAIL]" in capsys.readouterr().out

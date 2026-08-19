from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot.auth_code import _read_code, login_with_code, mask_login

pytestmark = pytest.mark.integration


def test_mask_login():
    assert mask_login("+79991234567") == "+79***4567"
    assert mask_login("person@example.com") == "p***@example.com"


class _Locator:
    def __init__(self, page, count=1, *, kind="field"):
        self.page = page
        self._count = count
        self.kind = kind

    def count(self):
        return self._count

    def click(self):
        if self.kind == "continue":
            self.page.stage = "credentials"
        elif self.kind == "submit":
            self.page.stage = "code"

    def check(self, **_kwargs):
        self.page.email_selected = True

    def fill(self, value):
        if self.kind == "code":
            self.page.code = value
            if value == "1234":
                self.page.context._cookies = [{"name": "hhtoken"}]

    def inner_text(self):
        return self.page.body


class _Context:
    def __init__(self, page):
        self.page = page
        self._cookies = []
        self.saved = None
        page.context = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def new_page(self):
        return self.page

    def cookies(self):
        return self._cookies

    def storage_state(self, *, path):
        self.saved = path


class _Page:
    def __init__(self, body=""):
        self.body = body
        self.stage = "start"
        self.email_selected = False
        self.code = None
        self.context = None

    def locator(self, selector):
        if selector == "[data-qa='submit-button']":
            return _Locator(self, kind="continue" if self.stage == "start" else "submit")
        if selector == "input[data-qa='credential-type-email']":
            return _Locator(self)
        if selector == "[data-qa='applicant-login-input-email']":
            return _Locator(self)
        if selector == "[data-qa='magritte-pincode-input-field']":
            return _Locator(self, count=int(self.stage == "code"), kind="code")
        if selector == "[data-qa='account-login-form']":
            return _Locator(self, count=0 if self.context and self.context._cookies else 1)
        if selector == "body":
            return _Locator(self)
        raise AssertionError(f"unexpected selector: {selector}")

    def wait_for_timeout(self, _milliseconds):
        return None


def _config(tmp_path):
    return type("Config", (), {"storage_state_file": tmp_path / "state.json", "user_agent": None})()


def test_login_with_code_keeps_one_context_and_saves_after_auth(
    monkeypatch, tmp_path, caplog, capsys
):
    page = _Page()
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)

    code_file = tmp_path / "code.txt"
    code_file.write_text("1234\n", encoding="utf-8")
    caplog.set_level(logging.INFO, logger="hhru_bot.auth_code")
    login_with_code(_config(tmp_path), "person@example.com", code_file=code_file, timeout_seconds=1)

    assert page.email_selected
    assert page.code == "1234"
    assert context.saved == str(tmp_path / "state.json")
    assert "person@example.com" not in caplog.text
    assert "1234" not in caplog.text
    assert "person@example.com" not in capsys.readouterr().out


def test_login_with_code_wrong_code_is_fail_closed(monkeypatch, tmp_path):
    page = _Page()
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)
    code_file = tmp_path / "code.txt"
    code_file.write_text("9999", encoding="utf-8")

    with pytest.raises(RuntimeError, match="не подтвердил вход"):
        login_with_code(
            _config(tmp_path), "person@example.com", code_file=code_file, timeout_seconds=0.01
        )
    assert context.saved is None


def test_login_with_code_browser_error_is_fail_closed(monkeypatch, tmp_path):
    page = _Page()
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)
    monkeypatch.setattr(
        _Locator, "fill", lambda *_args: (_ for _ in ()).throw(PlaywrightError("timeout"))
    )
    code_file = tmp_path / "code.txt"
    code_file.write_text("1234", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Ошибка браузера"):
        login_with_code(_config(tmp_path), "person@example.com", code_file=code_file)
    assert context.saved is None


def test_read_code_stdin_timeout(monkeypatch):
    monkeypatch.setattr("hhru_bot.auth_code.select.select", lambda *args: ([], [], []))
    with pytest.raises(RuntimeError, match="истёк"):
        _read_code(None, 300)


def test_credentials_are_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="hhru_bot.auth_code")
    logger = logging.getLogger("hhru_bot.auth_code")
    logger.info("login %s", mask_login("person@example.com"))
    assert "person@example.com" not in caplog.text
    assert "1234" not in caplog.text

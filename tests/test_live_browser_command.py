"""Тесты live-browser (#1209): браузер live-канала с нативной загрузкой сессии.

Реальный Playwright под стражем conftest — _launch_context/_auth_ok мокаются.
Проверяем: отказ без файла сессии/расширения, проброс storage_state и флагов
в launch, [FAIL] при непринятой сессии (гейт #1206), foreground-остановку по
EOF и живучесть при обрыве сети на стартовой вкладке.
"""

from __future__ import annotations

import argparse
import io
import json
import textwrap
from pathlib import Path

import pytest

from hhru_bot.commands import live_browser as live_browser_cmd

pytestmark = pytest.mark.unit


def _write_config(tmp_path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            account:
              storage_state_file: storage_state/hh_session.json
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/12345"
                search:
                  text: "python"
            """
        ),
        encoding="utf-8",
    )
    return str(path)


def _write_storage_state(tmp_path) -> Path:
    path = tmp_path / "storage_state" / "hh_session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    cookies = [
        {"name": "hhtoken", "value": "synthetic", "domain": ".hh.ru", "path": "/", "expires": -1}
    ]
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return path


def _args(tmp_path, **overrides) -> argparse.Namespace:
    base = dict(
        config=_write_config(tmp_path),
        profile_dir=tmp_path / "ext-profile",
        headless=False,
        url=live_browser_cmd.DEFAULT_START_URL,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class _FakeLocator:
    def __init__(self, count: int):
        self._count = count

    def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, url: str = "https://hh.ru/applicant/resumes", login_forms: int = 0):
        self.url = url
        self._login_forms = login_forms
        self.gotos: list[str] = []

    def goto(self, url: str, *, wait_until: str | None = None) -> None:
        self.gotos.append(url)

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self._login_forms)


class _FakeContext:
    def __init__(self, page: _FakePage):
        self._page = page
        self.pages: list = []
        self.closed = False
        self.added: list = []

    def new_page(self) -> _FakePage:
        return self._page

    def add_cookies(self, cookies: list) -> None:
        self.added = cookies

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    """Заглушка точки импорта: with sync_playwright() as p."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc) -> bool:
        return False


def _patch_pw(monkeypatch):
    monkeypatch.setattr(live_browser_cmd, "_sync_playwright", lambda: _FakePlaywright)


def _patch_launch(monkeypatch, context: _FakeContext) -> dict:
    seen: dict = {}

    def fake_launch(_p, profile_dir, headless, extension_dir):
        seen["profile_dir"] = profile_dir
        seen["headless"] = headless
        seen["extension_dir"] = extension_dir
        return context

    monkeypatch.setattr(live_browser_cmd, "_launch_context", fake_launch)
    return seen


def test_run_fails_without_storage_state_file(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path)  # файла storage_state нет

    def fail_launch(*_a, **_kw):
        raise AssertionError("без файла сессии браузер не запускается")

    monkeypatch.setattr(live_browser_cmd, "_launch_context", fail_launch)

    failed = live_browser_cmd.run(_args(tmp_path))

    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out


def test_run_fails_without_extension(tmp_path, monkeypatch, capsys):
    _write_storage_state(tmp_path)
    monkeypatch.setattr(live_browser_cmd, "_extension_dir", lambda: tmp_path / "no-such-ext")

    def fail_launch(*_a, **_kw):
        raise AssertionError("без manifest.json расширения запуск не нужен")

    monkeypatch.setattr(live_browser_cmd, "_launch_context", fail_launch)

    failed = live_browser_cmd.run(_args(tmp_path))

    assert failed is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "editable" in out


def test_run_launches_with_session_and_stops_on_eof(tmp_path, monkeypatch, capsys):
    _write_storage_state(tmp_path)
    page = _FakePage()
    context = _FakeContext(page)
    seen = _patch_launch(monkeypatch, context)
    _patch_pw(monkeypatch)
    monkeypatch.setattr(live_browser_cmd, "_auth_ok", lambda _page: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # EOF сразу — foreground-остановка

    failed = live_browser_cmd.run(_args(tmp_path))

    assert failed is False
    assert context.closed is True
    assert page.gotos == [live_browser_cmd.DEFAULT_START_URL]
    assert [c["name"] for c in context.added] == ["hhtoken"]  # куки сессии ушли в контекст
    assert seen["headless"] is False
    assert seen["extension_dir"].name == "hhru-live"
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "Ctrl+C" in out


def test_run_headless_flag_reaches_launch(tmp_path, monkeypatch, capsys):
    _write_storage_state(tmp_path)
    context = _FakeContext(_FakePage())
    seen = _patch_launch(monkeypatch, context)
    _patch_pw(monkeypatch)
    monkeypatch.setattr(live_browser_cmd, "_auth_ok", lambda _page: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    failed = live_browser_cmd.run(_args(tmp_path, headless=True))

    assert failed is False
    assert seen["headless"] is True


def test_run_fails_when_session_rejected(tmp_path, monkeypatch, capsys):
    # Гейт #1206: форма входа на hh.ru — сервер не принял сессию.
    _write_storage_state(tmp_path)
    page = _FakePage(login_forms=1)
    context = _FakeContext(page)
    _patch_launch(monkeypatch, context)
    _patch_pw(monkeypatch)

    failed = live_browser_cmd.run(_args(tmp_path))

    assert failed is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "[OK]" not in out
    assert context.closed is True


def test_run_survives_goto_network_error(tmp_path, monkeypatch, capsys):
    # Обрыв соединения на стартовой вкладке (локальная сеть, CLAUDE.md) —
    # браузер продолжает работать до EOF.
    _write_storage_state(tmp_path)
    page = _FakePage(url="about:blank")
    context = _FakeContext(page)
    _patch_launch(monkeypatch, context)
    _patch_pw(monkeypatch)

    def broken_goto(url, wait_until=None):
        page.url = url
        raise RuntimeError("net::ERR_CONNECTION_RESET")

    page.goto = broken_goto
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    failed = live_browser_cmd.run(_args(tmp_path))

    assert failed is False
    assert context.closed is True
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "не открылась" in out


def test_auth_ok_counts_login_form():
    assert live_browser_cmd._auth_ok(_FakePage(login_forms=0)) is True
    assert live_browser_cmd._auth_ok(_FakePage(login_forms=1)) is False


def test_launch_args_carry_extension_switches():
    ext = Path("/repo/extensions/hhru-live")
    args = live_browser_cmd._launch_args(ext)
    assert f"--load-extension={ext}" in args
    # Без except-флага Playwright добавляет --disable-extensions (#1209).
    assert f"--disable-extensions-except={ext}" in args
    assert "--disable-features=DisableLoadExtensionCommandLineSwitch" in args

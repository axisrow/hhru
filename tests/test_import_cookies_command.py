"""Тесты команды import-cookies (#166): контракт отказа при сбое Chrome.

Расшифровку/browser-cookie3 мокаем — реальный Keychain в CI недоступен;
проверяем, что команда не падает необработанным traceback'ом на ожидаемых
операционных ошибках Chrome (locked DB, decryption failure), а печатает
[FAIL] и возвращает True (провал по fail-closed конвенции cli.py).
"""

from __future__ import annotations

import argparse
import textwrap

import browser_cookie3
import pytest

from hhru_bot.commands import import_cookies as import_cookies_cmd


def _write_config(tmp_path, storage_state: str = "storage_state/hh_session.json") -> str:
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            account:
              storage_state_file: {storage_state}
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


def _args(config_path: str, **overrides) -> argparse.Namespace:
    base = {"config": config_path, "profile": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_run_reports_fail_on_browser_cookie_error(tmp_path, monkeypatch):
    # browser_cookie3.BrowserCookieError наследуется прямо от Exception (не
    # OSError/RuntimeError/ValueError), поэтому "голый" except в run() его не
    # перехватывал — locked/unreadable Chrome DB крашила CLI необработанным
    # traceback'ом вместо контролируемого отказа (Codex review, PR #168).
    def _raise_browser_cookie_error(_cookie_file):
        raise browser_cookie3.BrowserCookieError("could not find Chrome cookies database")

    import hhru_bot.cookie_import as cookie_import_mod

    monkeypatch.setattr(cookie_import_mod, "read_chrome_cookies", _raise_browser_cookie_error)

    args = _args(_write_config(tmp_path))
    failed = import_cookies_cmd.run(args)

    assert failed is True


def test_run_does_not_touch_existing_session_on_browser_cookie_error(tmp_path, monkeypatch):
    storage_state = tmp_path / "storage_state" / "hh_session.json"
    storage_state.parent.mkdir(parents=True)
    storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    def _raise_browser_cookie_error(_cookie_file):
        raise browser_cookie3.BrowserCookieError("decryption failed")

    import hhru_bot.cookie_import as cookie_import_mod

    monkeypatch.setattr(cookie_import_mod, "read_chrome_cookies", _raise_browser_cookie_error)

    args = _args(_write_config(tmp_path, storage_state=str(storage_state)))
    import_cookies_cmd.run(args)

    assert storage_state.read_text(encoding="utf-8") == '{"cookies": [], "origins": []}'
    assert not storage_state.with_name(storage_state.name + ".bak").exists()


@pytest.mark.parametrize("exc_type", [OSError, RuntimeError, ValueError])
def test_run_still_reports_fail_on_previously_handled_errors(tmp_path, monkeypatch, exc_type):
    def _raise(_cookie_file):
        raise exc_type("boom")

    import hhru_bot.cookie_import as cookie_import_mod

    monkeypatch.setattr(cookie_import_mod, "read_chrome_cookies", _raise)

    args = _args(_write_config(tmp_path))
    assert import_cookies_cmd.run(args) is True

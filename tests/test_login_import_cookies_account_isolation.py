"""Issue #725 (эпик #706): login и import-cookies пишут сессию в СВОЙ аккаунт.

Поведение уже реализовано: обе команды резолвят путь сессии через
`config.storage_state_file`, который `parse_account()` считает от директории
файла конфига (`config.py::load_config`, `config_sections/account.py`), а не
от cwd и не от какого-то общего расположения. Здесь это закрепляется тестом:
с двумя разными аккаунтами сессия аккаунта A должна попадать строго в файл
аккаунта A и не задевать файл аккаунта B.

Ни браузер, ни живой hh.ru не используются — `login()` мокает Playwright и
условие завершения ожидания входа, `import-cookies` мокает чтение куки
Chrome. Оба теста проверяют только резолв пути записи.
"""

from __future__ import annotations

import argparse
import textwrap
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hhru_bot import auth
from hhru_bot.commands import import_cookies as import_cookies_cmd
from hhru_bot.config import load_config

pytestmark = pytest.mark.integration


def _write_account_config(base_dir: Path, name: str, *, session_name: str) -> Path:
    """Отдельная директория аккаунта со своим config.yaml и storage_state путём.

    storage_state_file указан относительным путём — именно так это выглядит в
    config.example.yaml и в реальных data/accounts/<name>/config.yaml; резолв
    в абсолютный путь проверяет parse_account(), а не этот тест.
    """
    account_dir = base_dir / name
    account_dir.mkdir(parents=True)
    config_path = account_dir / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""\
            account:
              storage_state_file: storage_state/{session_name}
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/{name}RESUME"
                search:
                  text: "python"
            """
        ),
        encoding="utf-8",
    )
    return config_path


@contextmanager
def _fake_sync_playwright_confirmed_login():
    """Playwright-заглушка: вход считается уже подтверждённым (первый опрос)."""
    context = MagicMock(name="BrowserContext")
    context.storage_state.return_value = {"cookies": [], "origins": []}
    fake_browser = MagicMock(name="Browser")
    fake_browser.new_context.return_value = context
    playwright = MagicMock(name="Playwright")
    playwright.chromium.launch.return_value = fake_browser
    yield playwright


def _run_login(monkeypatch, config, tmp_path) -> None:
    monkeypatch.setattr(auth, "sync_playwright", lambda: _fake_sync_playwright_confirmed_login())
    monkeypatch.setattr(auth, "launch_browser", lambda p, **_kwargs: p.chromium.launch())
    monkeypatch.setattr(auth, "goto_hh", MagicMock())
    monkeypatch.setattr(auth, "has_auth_cookie", lambda _page: True)
    monkeypatch.setattr(auth, "has_login_form", lambda _page: False)
    monkeypatch.setattr(auth, "read_account_profile", MagicMock(return_value=0))

    auth.login(config, history_path=tmp_path / "unused_history.db")


def test_login_writes_session_only_to_own_account_path(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    config_a = load_config(
        _write_account_config(accounts_dir, "alpha", session_name="alpha_session.json")
    )
    config_b = load_config(
        _write_account_config(accounts_dir, "beta", session_name="beta_session.json")
    )

    assert config_a.storage_state_file != config_b.storage_state_file
    assert not config_a.storage_state_file.exists()
    assert not config_b.storage_state_file.exists()

    _run_login(monkeypatch, config_a, tmp_path)

    assert config_a.storage_state_file.exists(), "login не записал сессию в свой storage_state_file"
    assert config_a.storage_state_file.parent == accounts_dir / "alpha" / "storage_state"
    assert not config_b.storage_state_file.exists(), (
        "login записал сессию в путь чужого аккаунта (beta) — утечка секрета"
    )


def _write_config_for_import_cookies(tmp_path, name: str, *, session_name: str) -> str:
    account_dir = tmp_path / name
    account_dir.mkdir(parents=True)
    config_path = account_dir / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""\
            account:
              storage_state_file: storage_state/{session_name}
            resumes:
              - id: r1
                resume_url: "https://hh.ru/resume/{name}RESUME"
                search:
                  text: "python"
            """
        ),
        encoding="utf-8",
    )
    return str(config_path)


def _import_cookies_args(config_path: str) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, profile=None)


def _fake_cookie_db_row(hhtoken_value: str = "token-value") -> dict:
    """Row shape read_chrome_cookies() yields — see build_storage_state()."""
    return {
        "host_key": ".hh.ru",
        "name": "hhtoken",
        "value": hhtoken_value,
        "path": "/",
        "expires_utc": 0,  # 0 -> chrome_expires_to_playwright() returns -1 (session cookie)
        "is_httponly": True,
        "is_secure": True,
        "samesite": -1,
    }


def test_import_cookies_writes_session_only_to_own_account_path(tmp_path, monkeypatch):
    config_path_a = _write_config_for_import_cookies(tmp_path, "alpha", session_name="alpha.json")
    config_path_b = _write_config_for_import_cookies(tmp_path, "beta", session_name="beta.json")
    config_a = load_config(config_path_a)
    config_b = load_config(config_path_b)

    assert config_a.storage_state_file != config_b.storage_state_file

    import hhru_bot.cookie_import as cookie_import_mod

    monkeypatch.setattr(
        cookie_import_mod,
        "read_chrome_cookies",
        lambda _cookie_file: [_fake_cookie_db_row()],
    )

    failed = import_cookies_cmd.run(_import_cookies_args(config_path_a))

    assert failed is False
    assert config_a.storage_state_file.exists(), (
        "import-cookies не записал сессию в свой storage_state_file"
    )
    assert config_a.storage_state_file.parent == tmp_path / "alpha" / "storage_state", (
        f"storage_state_file резолвится не от директории конфига своего аккаунта: "
        f"{config_a.storage_state_file}"
    )
    assert not config_b.storage_state_file.exists(), (
        "import-cookies записал сессию в путь чужого аккаунта (beta) — утечка секрета"
    )

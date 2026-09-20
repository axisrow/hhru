"""Тесты команды session-seed (#1195): посев кук storage_state в профиль браузера.

Реальный launch_persistent_context мокается (на уровне _seed_cookies) —
в CI Chromium не запускаем. Проверяем fail-closed гейты (нет файла, нет
hhtoken — профиль не мутируется), передачу кук и вывод по конвенции.
"""

from __future__ import annotations

import argparse
import json
import textwrap

import pytest

from hhru_bot.commands import session_seed as session_seed_cmd

pytestmark = pytest.mark.integration


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


def _write_storage_state(tmp_path, cookies: list[dict]):
    path = tmp_path / "storage_state" / "hh_session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return path


def _args(config_path: str, profile_dir) -> argparse.Namespace:
    return argparse.Namespace(config=config_path, profile_dir=profile_dir)


def _hhtoken_cookie(expires: float | int = -1) -> dict:
    return {
        "name": "hhtoken",
        "value": "abc",
        "domain": ".hh.ru",
        "path": "/",
        "expires": expires,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def test_run_seeds_cookies_into_profile(tmp_path, monkeypatch, capsys):
    state = _write_storage_state(tmp_path, [_hhtoken_cookie(), {"name": "x", "value": "y"}])
    profile = tmp_path / "worker-profile"
    seen: dict = {}

    def fake_seed(profile_dir, cookies):
        seen["profile_dir"] = profile_dir
        seen["cookies"] = cookies

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fake_seed)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), profile))

    assert failed is False
    assert seen["profile_dir"] == profile
    assert len(seen["cookies"]) == 2
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "hhtoken" in out
    assert str(state) not in out  # сеем в профиль, storage_state не упоминаем как результат


def test_run_fails_without_storage_state_file(tmp_path, monkeypatch, capsys):
    def fail_seed(_profile_dir, _cookies):
        raise AssertionError("профиль не должен открываться без сессии")

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fail_seed)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out


def test_run_fail_closed_without_hhtoken(tmp_path, monkeypatch, capsys):
    _write_storage_state(tmp_path, [{"name": "other", "value": "x"}])
    profile = tmp_path / "p"

    def fail_seed(_profile_dir, _cookies):
        raise AssertionError("без hhtoken профиль мутироваться не должен")

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fail_seed)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), profile))

    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out
    assert not profile.exists()


def test_run_fail_closed_on_expired_hhtoken(tmp_path, monkeypatch, capsys):
    # expires в прошлом (не -1) — та же проверка свежести, что у import-cookies.
    _write_storage_state(tmp_path, [_hhtoken_cookie(expires=1_000)])

    def fail_seed(_profile_dir, _cookies):
        raise AssertionError("просроченный hhtoken — сеять нельзя")

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fail_seed)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out


def test_run_reports_fail_on_seed_error(tmp_path, monkeypatch, capsys):
    _write_storage_state(tmp_path, [_hhtoken_cookie()])

    def broken_seed(_profile_dir, _cookies):
        raise RuntimeError("profile is locked by another Chrome")

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", broken_seed)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out


def test_run_reports_fail_on_malformed_storage_state(tmp_path, monkeypatch, capsys):
    path = tmp_path / "storage_state" / "hh_session.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"origins": []}', encoding="utf-8")

    def fail_seed(_profile_dir, _cookies):
        raise AssertionError("битый storage_state — профиль не открывать")

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fail_seed)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out

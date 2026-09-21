"""Тесты команды session-seed (#1195): посев кук storage_state в профиль браузера.

Реальный launch_persistent_context мокается (на уровне _seed_cookies и
_verify_token_survives_restart) — живой контур гоняет чеклист и эксперименты
из #1206, в тестах Playwright под стражем conftest. Проверяем fail-closed
(нет файла, нет hhtoken, сеянный токен не пережил рестарт — профиль не
считается засеянным) и нормализацию expires (#1206).
"""

from __future__ import annotations

import argparse
import json
import textwrap
import time

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


def _write_storage_state(tmp_path, cookies: list):
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


def _mock_verify_ok(monkeypatch):
    monkeypatch.setattr(
        session_seed_cmd,
        "_verify_token_survives_restart",
        lambda _profile_dir: (True, "hhtoken постоянный, переживает рестарт браузера"),
    )


def test_run_seeds_cookies_into_profile(tmp_path, monkeypatch, capsys):
    state = _write_storage_state(tmp_path, [_hhtoken_cookie(), {"name": "x", "value": "y"}])
    profile = tmp_path / "worker-profile"
    seen: dict = {}

    def fake_seed(profile_dir, cookies):
        seen["profile_dir"] = profile_dir
        seen["cookies"] = cookies

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fake_seed)
    _mock_verify_ok(monkeypatch)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), profile))

    assert failed is False
    assert seen["profile_dir"] == profile
    assert len(seen["cookies"]) == 2
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "hhtoken" in out
    assert str(state) not in out  # сеем в профиль, storage_state не упоминаем как результат


def test_run_normalizes_cookies_before_seed(tmp_path, monkeypatch, capsys):
    # #1206: сессионные/истёкшие куки сеются с конечным сроком, лишние ключи
    # (session из стороннего экспорта) не доходят до add_cookies.
    now = time.time()
    _write_storage_state(
        tmp_path,
        [
            _hhtoken_cookie(expires=-1),
            {**_hhtoken_cookie(), "name": "expired", "expires": now - 100},
            {
                "name": "extra",
                "value": "v",
                "domain": ".hh.ru",
                "path": "/",
                "expires": now + 100,
                "session": None,
            },
        ],
    )
    seen: dict = {}

    def fake_seed(_profile_dir, cookies):
        seen["cookies"] = cookies

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fake_seed)
    _mock_verify_ok(monkeypatch)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is False
    by_name = {c["name"]: c for c in seen["cookies"]}
    assert set(seen["cookies"][0].keys()) <= {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
        "partitionKey",
    }
    assert "session" not in by_name["extra"]
    assert by_name["hhtoken"]["expires"] > now
    assert by_name["expired"]["expires"] > now
    assert by_name["extra"]["expires"] == pytest.approx(now + 100)
    assert "[OK]" in capsys.readouterr().out


def test_normalize_for_seed_pure():
    now = 1_000_000.0
    normalized = session_seed_cmd._normalize_for_seed(
        [
            {
                "name": "persistent",
                "value": "v",
                "domain": ".hh.ru",
                "path": "/",
                "expires": now + 50,
            },
            {"name": "session", "value": "v", "domain": ".hh.ru", "path": "/", "expires": -1},
            {"name": "noexp", "value": "v", "domain": ".hh.ru", "path": "/"},
            {
                "name": "junk",
                "value": "v",
                "domain": ".hh.ru",
                "path": "/",
                "expires": now + 50,
                "session": True,
            },
        ],
        now,
    )
    by_name = {c["name"]: c for c in normalized}
    assert by_name["persistent"]["expires"] == now + 50
    assert by_name["session"]["expires"] == pytest.approx(now + 30 * 24 * 3600)
    assert by_name["noexp"]["expires"] == pytest.approx(now + 30 * 24 * 3600)
    assert "session" not in by_name["junk"]


def test_run_skips_non_dict_cookie_elements(tmp_path, monkeypatch, capsys):
    # Не-dict элемент списка рядом с валидным hhtoken (гейт пропускает: dict
    # требуется только для поиска hhtoken) не должен ронять нормализацию
    # TypeError'ом — мусор отбрасывается, валидные куки сеются.
    _write_storage_state(tmp_path, [_hhtoken_cookie(), 42])
    seen: dict = {}

    def fake_seed(_profile_dir, cookies):
        seen["cookies"] = cookies

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fake_seed)
    _mock_verify_ok(monkeypatch)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is False
    assert [c["name"] for c in seen["cookies"]] == ["hhtoken"]
    assert "[OK]" in capsys.readouterr().out


def test_run_reports_fail_when_restart_guard_crashes(tmp_path, monkeypatch, capsys):
    # Страж readback'а — тот же класс отказов, что и посев (профиль в этот
    # момент занят другим Chrome и т.п.): честный [FAIL], а не сырой traceback.
    _write_storage_state(tmp_path, [_hhtoken_cookie()])

    def broken_verify(_profile_dir):
        raise RuntimeError("profile is locked by another Chrome")

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", lambda _p, _c: None)
    monkeypatch.setattr(session_seed_cmd, "_verify_token_survives_restart", broken_verify)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "[OK]" not in out


def test_run_fails_when_seeded_token_does_not_survive_restart(tmp_path, monkeypatch, capsys):
    # #1206: посев [OK], но токен после перезапуска профиля session/нет —
    # команда обязана ответить [FAIL], а не молча отчитаться успехом.
    _write_storage_state(tmp_path, [_hhtoken_cookie()])

    def fake_seed(_profile_dir, _cookies):
        pass

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fake_seed)
    monkeypatch.setattr(
        session_seed_cmd,
        "_verify_token_survives_restart",
        lambda _profile_dir: (
            False,
            "hhtoken в профиле session/истёк — посев не переживёт рестарт браузера",
        ),
    )

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "[OK]" not in out


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


@pytest.mark.parametrize(
    "payload",
    [
        '{"origins": []}',  # нет ключа cookies -> KeyError
        "[1, 2]",  # валидный JSON, но не объект -> TypeError
        '{"cookies": "x"}',  # cookies не список -> ValueError
    ],
)
def test_run_reports_fail_on_malformed_storage_state(tmp_path, monkeypatch, capsys, payload):
    path = tmp_path / "storage_state" / "hh_session.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    def fail_seed(_profile_dir, _cookies):
        raise AssertionError("битый storage_state — профиль не открывать")

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fail_seed)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out


def test_run_fail_closed_on_non_dict_cookie_element(tmp_path, monkeypatch, capsys):
    # Валидный JSON, но элемент списка — не dict: .get() в hhtoken-гейте
    # уронил бы AttributeError мимо except (review PR #1201); должен быть
    # честный [FAIL] «hhtoken не найден» без traceback.
    _write_storage_state(tmp_path, ["not-a-dict"])

    def fail_seed(_profile_dir, _cookies):
        raise AssertionError("кривые cookies — профиль не открывать")

    monkeypatch.setattr(session_seed_cmd, "_seed_cookies", fail_seed)

    failed = session_seed_cmd.run(_args(_write_config(tmp_path), tmp_path / "p"))

    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out

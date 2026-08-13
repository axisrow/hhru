from __future__ import annotations

import json
from pathlib import Path

import pytest

from hhru_bot.cookie_import import (
    build_storage_state,
    chrome_expires_to_playwright,
    chrome_samesite_to_playwright,
    write_storage_state,
)


def row(**overrides):
    value = {
        "host_key": ".hh.ru",
        "name": "hhtoken",
        "value": "secret",
        "path": "/",
        "expires_utc": 0,
        "is_httponly": 1,
        "is_secure": 1,
        "samesite": -1,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("source", "expected"),
    [(0, -1), (11_644_473_600_000_000, 0), (11_644_473_601_000_000, 1)],
)
def test_chrome_expiration_conversion(source, expected):
    assert chrome_expires_to_playwright(source) == expected


def test_chrome_expiration_rejects_overflow():
    with pytest.raises(ValueError):
        chrome_expires_to_playwright(10**400)


def test_samesite_mapping():
    assert [chrome_samesite_to_playwright(value) for value in (-1, 0, 1, 2)] == [
        "Lax",
        "None",
        "Lax",
        "Strict",
    ]


def test_storage_state_shape_and_private_domain_guard():
    state = build_storage_state([row(), row(host_key="mail.example.com", name="password")])
    assert state == {
        "cookies": [
            {
                "name": "hhtoken",
                "value": "secret",
                "domain": ".hh.ru",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def test_backup_does_not_overwrite_existing_backup(tmp_path: Path):
    destination = tmp_path / "hh_session.json"
    backup = tmp_path / "hh_session.json.bak"
    destination.write_text('{"old": 1}', encoding="utf-8")
    backup.write_text("keep", encoding="utf-8")

    created_backup = write_storage_state({"cookies": [], "origins": []}, destination)

    assert created_backup == tmp_path / "hh_session.json.bak.1"
    assert backup.read_text(encoding="utf-8") == "keep"
    assert json.loads(destination.read_text(encoding="utf-8")) == {"cookies": [], "origins": []}

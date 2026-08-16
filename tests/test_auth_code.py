from __future__ import annotations

import logging

import pytest

from hhru_bot.auth_code import mask_login, submit_code

pytestmark = pytest.mark.integration


def test_mask_login():
    assert mask_login("+79991234567") == "+79***4567"
    assert mask_login("person@example.com") == "p***@example.com"


def test_credentials_are_not_logged(caplog, tmp_path):
    login = "+79991234567"
    code = "123456"
    caplog.set_level(logging.INFO, logger="hhru_bot.auth_code")
    with pytest.raises(RuntimeError):
        submit_code(
            type(
                "Config", (), {"storage_state_file": tmp_path / "state.json", "user_agent": None}
            )(),
            code,
        )
    assert login not in caplog.text
    assert code not in caplog.text

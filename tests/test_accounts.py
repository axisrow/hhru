"""Unit tests for named-account path resolution."""

from pathlib import Path

import pytest

from hhru_bot.accounts import AccountError, AccountPaths, resolve_account_paths

pytestmark = pytest.mark.unit


def test_resolves_config_and_history_for_existing_account(tmp_path: Path):
    config = tmp_path / "accounts" / "marketing" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("account: {}\n")

    assert resolve_account_paths("marketing", data_dir=tmp_path) == AccountPaths(
        config=config,
        history=config.parent / "history.db",
    )


def test_history_does_not_have_to_exist(tmp_path: Path):
    account = tmp_path / "accounts" / "new"
    account.mkdir(parents=True)
    (account / "config.yaml").touch()

    assert resolve_account_paths("new", data_dir=tmp_path).history == account / "history.db"


def test_missing_account_is_explicit_error(tmp_path: Path):
    with pytest.raises(AccountError, match="аккаунт 'missing' не найден"):
        resolve_account_paths("missing", data_dir=tmp_path)

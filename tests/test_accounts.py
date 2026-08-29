"""Unit tests for named-account path resolution."""

from pathlib import Path

import pytest

from hhru_bot.accounts import (
    AccountError,
    AccountPaths,
    resolve_account_paths,
    validate_account_name,
)

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


# -- #741 finding 1: reject account names that escape data_dir/accounts -----


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".",
        "",
        "../../foo",
        "/etc/passwd",
        "foo/bar",
        "foo/../../bar",
        "a/../b",
    ],
)
def test_validate_account_name_rejects_traversal_and_separators(name: str):
    with pytest.raises(AccountError, match="недопустимое имя аккаунта"):
        validate_account_name(name)


def test_validate_account_name_accepts_plain_name():
    validate_account_name("marketing")


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../../foo",
        "/etc/passwd",
        "foo/bar",
    ],
)
def test_resolve_account_paths_rejects_traversal_before_touching_filesystem(
    tmp_path: Path, name: str
):
    # An external directory with its own config.yaml must never be reachable
    # through a crafted --account value (issue #741 finding 1): a config.yaml
    # placed outside data_dir/accounts must not resolve at all.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.yaml").touch()

    with pytest.raises(AccountError, match="недопустимое имя аккаунта"):
        resolve_account_paths(name, data_dir=tmp_path)


def test_resolve_account_paths_rejects_symlink_escaping_accounts_root(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.yaml").touch()

    accounts_dir = tmp_path / "data" / "accounts"
    accounts_dir.mkdir(parents=True)
    (accounts_dir / "escape").symlink_to(outside)

    with pytest.raises(AccountError):
        resolve_account_paths("escape", data_dir=tmp_path / "data")

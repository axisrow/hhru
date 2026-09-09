"""Unit tests for named-account path resolution."""

from pathlib import Path

import pytest

from hhru_bot.accounts import (
    AccountError,
    AccountPaths,
    read_default_account,
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


# --- read_default_account (#1086) ---


def test_read_default_account_returns_name(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("default_account: marketing\n", encoding="utf-8")
    assert read_default_account(config) == "marketing"


def test_read_default_account_strips_whitespace(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text('default_account: "  marketing  "\n', encoding="utf-8")
    assert read_default_account(config) == "marketing"


@pytest.mark.parametrize(
    "text",
    [
        "",  # пустой файл
        "resumes: []\n",  # поля нет
        "default_account:\n",  # ключ есть, значения нет
        "default_account: ''\n",
        "# default_account: marketing\n",  # закомментирован
    ],
)
def test_read_default_account_absent_value_is_none(tmp_path: Path, text: str):
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    assert read_default_account(config) is None


def test_read_default_account_missing_file_is_none(tmp_path: Path):
    assert read_default_account(tmp_path / "config.yaml") is None


@pytest.mark.parametrize("value", ["[marketing]", "42", "{name: marketing}"])
def test_read_default_account_non_string_fails(tmp_path: Path, value: str):
    config = tmp_path / "config.yaml"
    config.write_text(f"default_account: {value}\n", encoding="utf-8")
    with pytest.raises(AccountError, match="default_account"):
        read_default_account(config)


def test_read_default_account_broken_yaml_fails_explicitly(tmp_path: Path):
    """Синтаксически битый корневой конфиг — AccountError (аккуратный [FAIL]
    через cli.main), не сырой yaml-traceback из _resolve_paths; молчаливый
    None скрыл бы поломку fallback'ом на корневые дефолты."""
    config = tmp_path / "config.yaml"
    config.write_text("default_account: [unclosed\n", encoding="utf-8")
    with pytest.raises(AccountError, match="не удалось прочитать"):
        read_default_account(config)

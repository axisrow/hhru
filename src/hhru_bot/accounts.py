"""Resolution of named account paths for the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class AccountError(ValueError):
    """A requested account cannot be resolved."""


@dataclass(frozen=True)
class AccountPaths:
    config: Path
    history: Path


def validate_account_name(name: str) -> None:
    """Reject account names that are not a single plain path component.

    A single ``Path(name).name != name`` check rejects empty strings,
    absolute paths, ``..``/``.`` traversal segments and any embedded path
    separator in one shot (see the equivalence table exercised in
    ``tests/test_accounts.py``) -- the same rule ``account create`` already
    applies. Kept here as the single source of truth so ``resolve_account_paths``
    (``--account`` on every command) enforces it too, not only account
    creation (#741).
    """
    if not name or name in {".", ".."} or Path(name).name != name:
        raise AccountError(f"недопустимое имя аккаунта: {name!r}")


def resolve_account_paths(
    name: str,
    *,
    data_dir: Path = Path("data"),
) -> AccountPaths:
    """Resolve an existing named account under ``data_dir/accounts``.

    ``history.db`` is intentionally not required to exist: ``History`` creates
    it on first use. The config is the account's existence marker and must be
    present before a command is started.

    The name is validated to be a single plain path component and the
    resolved account directory is verified to actually stay inside
    ``data_dir/accounts`` before any caller (notably ``session_security``'s
    POSIX-mode hardening) is allowed to treat it as the managed account
    directory (#741). Without this, ``--account ../../foo`` pointing at an
    external directory that happens to contain a ``config.yaml`` would let
    filesystem hardening chmod an unrelated, attacker- or victim-owned path.
    """
    validate_account_name(name)
    accounts_root = (data_dir / "accounts").resolve()
    account_dir = data_dir / "accounts" / name
    resolved_account_dir = account_dir.resolve()
    if resolved_account_dir != accounts_root and accounts_root not in resolved_account_dir.parents:
        raise AccountError(f"недопустимое имя аккаунта: {name!r}")
    config = account_dir / "config.yaml"
    if not config.is_file():
        raise AccountError(f"аккаунт '{name}' не найден: {config}")
    return AccountPaths(config=config, history=account_dir / "history.db")

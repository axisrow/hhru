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


def resolve_account_paths(
    name: str,
    *,
    data_dir: Path = Path("data"),
) -> AccountPaths:
    """Resolve an existing named account under ``data_dir/accounts``.

    ``history.db`` is intentionally not required to exist: ``History`` creates
    it on first use. The config is the account's existence marker and must be
    present before a command is started.
    """
    account_dir = data_dir / "accounts" / name
    config = account_dir / "config.yaml"
    if not config.is_file():
        raise AccountError(f"аккаунт '{name}' не найден: {config}")
    return AccountPaths(config=config, history=account_dir / "history.db")

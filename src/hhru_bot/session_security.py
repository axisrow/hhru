"""Local filesystem protections for hh.ru session secrets."""

from __future__ import annotations

import os
from pathlib import Path

SESSION_FILE_MODE = 0o600
ACCOUNT_DIR_MODE = 0o700


def permissions_are_posix() -> bool:
    """Return whether Unix mode bits are meaningful for this process."""
    return os.name != "nt"


def secure_directory(path: Path, mode: int = ACCOUNT_DIR_MODE, *, exist_ok: bool = True) -> None:
    """Create a directory and tighten its mode where Unix modes are supported."""
    path.mkdir(parents=True, exist_ok=exist_ok, mode=mode)
    if permissions_are_posix():
        os.chmod(path, mode)


def _account_directory(path: Path) -> Path | None:
    for parent in (path.parent, *path.parent.parents):
        if parent.parent.name == "accounts":
            return parent
    return None


def secure_storage_state_parent(destination: Path | str) -> Path:
    """Prepare private directories surrounding a storage-state file.

    The account directory is tightened when the destination belongs to the
    standard ``data/accounts/<name>`` layout.  The immediate storage directory
    is private too, including for custom paths, so a session cannot be listed
    by another local user while it is being written.
    """
    destination = Path(destination)
    secure_directory(destination.parent)
    account_directory = _account_directory(destination)
    if account_directory is not None:
        secure_directory(account_directory)
    return destination


def prepare_storage_state_file(destination: Path | str) -> bool:
    """Ensure a session destination exists with owner-only mode.

    Returning whether this call created the file lets callers remove an empty
    placeholder if the subsequent browser write fails.  On Windows the mode
    argument and ``chmod`` do not represent ACLs; directory preparation still
    succeeds, while diagnostics explicitly reports that ACL inspection is not
    available.
    """
    destination = secure_storage_state_parent(destination)
    if not permissions_are_posix():
        return False

    try:
        os.chmod(destination, SESSION_FILE_MODE)
    except FileNotFoundError:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(destination, flags, SESSION_FILE_MODE)
        except FileExistsError:
            os.chmod(destination, SESSION_FILE_MODE)
        else:
            os.close(fd)
            return True
    return False


def secure_storage_state_file(destination: Path | str) -> None:
    """Tighten an existing session file after a writer has populated it."""
    if permissions_are_posix():
        os.chmod(Path(destination), SESSION_FILE_MODE)

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
        # Only the repository's standard ``data/accounts/<name>`` layout is
        # managed here.  A user may intentionally configure a custom path
        # such as ``/srv/accounts/shared``; chmod must not lock that directory.
        if parent.parent.name == "accounts" and parent.parent.parent.name == "data":
            return parent
    return None


def _open_without_follow(path: Path, flags: int) -> int:
    """Open a session path without following a symlink on POSIX."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        raise OSError(f"путь сессии является символической ссылкой: {path}")
    return os.open(path, flags | nofollow)


def secure_storage_state_parent(destination: Path | str) -> Path:
    """Prepare private directories surrounding a storage-state file.

    The account directory is tightened when the destination belongs to the
    standard ``data/accounts/<name>`` layout.  A missing storage directory is
    created privately; existing custom directories are left untouched because
    the caller may share them with unrelated processes.
    """
    destination = Path(destination)
    # Do not chmod an arbitrary existing path supplied by a user.  For
    # example, a custom ``/tmp/hh_session.json`` must not turn shared /tmp
    # into a private directory.  A missing parent is ours to create, so it can
    # safely start private; the standard account directory is tightened below.
    parent_exists = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=ACCOUNT_DIR_MODE)
    if not parent_exists and permissions_are_posix():
        os.chmod(destination.parent, ACCOUNT_DIR_MODE)
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
        fd = _open_without_follow(destination, os.O_RDONLY)
    except FileNotFoundError:
        flags = os.O_WRONLY | os.O_CREAT
        try:
            fd = _open_without_follow(destination, flags | os.O_EXCL)
        except FileExistsError:
            fd = _open_without_follow(destination, os.O_RDONLY)
        else:
            try:
                os.fchmod(fd, SESSION_FILE_MODE)
            finally:
                os.close(fd)
            return True
    try:
        os.fchmod(fd, SESSION_FILE_MODE)
    finally:
        os.close(fd)
    return False


def secure_storage_state_file(destination: Path | str) -> None:
    """Tighten an existing session file after a writer has populated it."""
    if permissions_are_posix():
        fd = _open_without_follow(Path(destination), os.O_RDONLY)
        try:
            os.fchmod(fd, SESSION_FILE_MODE)
        finally:
            os.close(fd)

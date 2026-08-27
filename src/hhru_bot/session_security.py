"""Local filesystem protections for hh.ru session secrets."""

from __future__ import annotations

import os
import tempfile
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


def _open_without_follow(path: Path, flags: int) -> int:
    """Open a session path without following a symlink on POSIX."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        raise OSError(f"путь сессии является символической ссылкой: {path}")
    return os.open(path, flags | nofollow)


def secure_storage_state_parent(
    destination: Path | str, *, account_dir: Path | str | None = None
) -> Path:
    """Prepare private directories surrounding a storage-state file.

    ``account_dir`` is supplied by the account-aware CLI path, rather than
    inferred from a user-controlled session path.  A missing storage directory
    is created privately; existing custom directories are left untouched
    because the caller may share them with unrelated processes.
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
    if account_dir is not None:
        secure_directory(Path(account_dir))
    return destination


def secure_storage_state_file(destination: Path | str) -> None:
    """Tighten an existing session file after a writer has populated it."""
    if permissions_are_posix():
        fd = _open_without_follow(Path(destination), os.O_RDONLY)
        try:
            os.fchmod(fd, SESSION_FILE_MODE)
        finally:
            os.close(fd)


def create_storage_state_temp(
    destination: Path | str, *, account_dir: Path | str | None = None
) -> tuple[int, Path]:
    """Create a private temporary path for a browser state export."""
    destination = secure_storage_state_parent(destination, account_dir=account_dir)
    fd, name = tempfile.mkstemp(
        dir=destination.parent, prefix=destination.name + ".", suffix=".tmp"
    )
    if permissions_are_posix():
        try:
            os.fchmod(fd, SESSION_FILE_MODE)
        except BaseException:
            os.close(fd)
            Path(name).unlink(missing_ok=True)
            raise
    return fd, Path(name)

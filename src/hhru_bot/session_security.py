"""Local filesystem protections for hh.ru session secrets."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

SESSION_FILE_MODE = 0o600
ACCOUNT_DIR_MODE = 0o700


def permissions_are_posix() -> bool:
    """Return whether Unix mode bits are meaningful for this process."""
    return os.name != "nt"


def secure_directory(path: Path, mode: int = ACCOUNT_DIR_MODE, *, exist_ok: bool = True) -> None:
    """Create a directory and tighten its mode where Unix modes are supported.

    When any part of ``path`` is missing, creation and hardening of every new
    component go through ``_mkdir_and_harden_new_components`` (descriptor-relative,
    no-follow) rather than ``Path.mkdir(parents=True)`` -- the latter leaves the
    same TOCTOU window on intermediate components that this module's other
    callers are hardened against (#741). When ``path`` already exists, only its
    own no-follow open + fchmod is needed, since no new component is created.
    """
    if permissions_are_posix() and not path.exists():
        _mkdir_and_harden_new_components(path, mode, exist_ok=exist_ok)
        return
    path.mkdir(parents=True, exist_ok=exist_ok, mode=mode)
    if permissions_are_posix():
        fd = _open_without_follow(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)


def _mkdir_and_harden_new_components(path: Path, mode: int, *, exist_ok: bool = True) -> None:
    """Create the missing suffix of ``path`` and chmod only what we created.

    ``Path.mkdir(parents=True)`` followed by a path-based ``os.chmod`` leaves a
    TOCTOU window: between a directory being created and the mode change, any
    path component -- not only the final one -- can be replaced with a
    symlink, redirecting the hardening to an unrelated target (#741). This
    walks up from ``path`` to find the nearest already-existing ancestor, then
    walks back down creating each missing component with ``os.mkdir(...,
    dir_fd=...)`` and hardening it with an ``O_NOFOLLOW``-opened descriptor
    (``os.fchmod``) before descending further -- so no step ever re-resolves a
    mutable pathname, and an ancestor that already existed before this call
    (which may be shared with other processes) is never touched.

    ``exist_ok`` mirrors ``Path.mkdir``'s parameter of the same name: when
    ``False``, the final component turning out to already exist by the time
    this walk reaches it (created by a racing process between the caller's
    existence check and this call) raises ``FileExistsError`` instead of
    being silently accepted -- otherwise ``secure_directory(..., exist_ok=False)``
    (account creation's rewrite-protection, ``create_account``) would lose its
    create-only guarantee purely because the target was fully missing when
    checked (cycle-review round 2 finding).

    Every *non-final* component racing into existence between the ancestor
    walk above and this loop's own ``os.mkdir`` call is rejected unconditionally
    (``exist_ok`` does not extend to it): silently descending into a directory
    this call did not create -- even one merely raced into place by another
    local process, not necessarily a symlink -- would mean hardening and
    building the rest of the tree inside a directory this function does not
    control, defeating "only components we created are ours to harden."
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_directory = getattr(os, "O_DIRECTORY", 0)
    resolved = path.absolute()

    # Find the nearest already-existing ancestor; only components below it
    # are ours to create and harden.
    existing = resolved
    missing: list[str] = []
    while not existing.exists():
        missing.append(existing.name)
        parent = existing.parent
        if parent == existing:
            break
        existing = parent
    missing.reverse()

    dir_fd = _open_without_follow(existing, os.O_RDONLY | o_directory)
    try:
        for index, component in enumerate(missing):
            is_final = index == len(missing) - 1
            try:
                os.mkdir(component, mode, dir_fd=dir_fd)
            except FileExistsError:
                if not is_final or not exist_ok:
                    raise
            next_fd = os.open(component, os.O_RDONLY | o_directory | nofollow, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
            os.fchmod(dir_fd, mode)
    finally:
        os.close(dir_fd)


def _open_without_follow(path: Path, flags: int) -> int:
    """Open a filesystem path without following a symlink on POSIX."""
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
    if account_dir is not None:
        secure_directory(Path(account_dir))

    # Do not chmod an arbitrary existing path supplied by a user.  For
    # example, a custom ``/tmp/hh_session.json`` must not turn shared /tmp
    # into a private directory.  A missing parent is ours to create, so it can
    # safely start private; the standard account directory is tightened below.
    parent_exists = destination.parent.exists()
    if permissions_are_posix() and not parent_exists:
        _mkdir_and_harden_new_components(destination.parent, ACCOUNT_DIR_MODE)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=ACCOUNT_DIR_MODE)
    return destination


def secure_storage_state_file(destination: Path | str) -> None:
    """Tighten an existing session file after a writer has populated it."""
    if permissions_are_posix():
        fd = _open_without_follow(Path(destination), os.O_RDONLY)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"файл сессии не является обычным файлом: {destination}")
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

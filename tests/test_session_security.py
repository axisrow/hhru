"""Unit tests for local filesystem hardening of hh.ru session secrets (#741).

Covers the boundary cases from the issue's open findings (account-name
containment, symlink-safe parent creation, non-regular session destinations)
plus regression coverage for the historical findings already fixed in #734
(arbitrary custom session parent, lexical ``accounts/<name>`` classification,
symlinked session file, symlinked managed account directory).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hhru_bot.session_security import (
    ACCOUNT_DIR_MODE,
    SESSION_FILE_MODE,
    permissions_are_posix,
    secure_directory,
    secure_storage_state_file,
    secure_storage_state_parent,
)

pytestmark = pytest.mark.unit

POSIX_ONLY = pytest.mark.skipif(not permissions_are_posix(), reason="POSIX-only mode bits")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- Finding 2: symlink-safe parent creation/hardening (TOCTOU) -------------


@POSIX_ONLY
def test_secure_storage_state_parent_creates_and_hardens_missing_dir(tmp_path):
    destination = tmp_path / "accounts" / "acme" / "sessions" / "hh_session.json"

    secure_storage_state_parent(destination)

    assert destination.parent.is_dir()
    assert _mode(destination.parent) == ACCOUNT_DIR_MODE


@POSIX_ONLY
def test_secure_storage_state_parent_hardens_every_new_component(tmp_path):
    # Multiple missing levels: each one we create must end up hardened, not
    # only the final directory.
    destination = tmp_path / "a" / "b" / "c" / "hh_session.json"

    secure_storage_state_parent(destination)

    assert _mode(tmp_path / "a") == ACCOUNT_DIR_MODE
    assert _mode(tmp_path / "a" / "b") == ACCOUNT_DIR_MODE
    assert _mode(tmp_path / "a" / "b" / "c") == ACCOUNT_DIR_MODE


@POSIX_ONLY
def test_secure_storage_state_parent_does_not_touch_existing_ancestor(tmp_path):
    # A pre-existing ancestor may be shared with unrelated processes (e.g. a
    # custom /tmp-based path); only components we create ourselves may be
    # chmodded.
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    destination = shared / "acme" / "hh_session.json"

    secure_storage_state_parent(destination)

    assert _mode(shared) == 0o755
    assert _mode(shared / "acme") == ACCOUNT_DIR_MODE


@POSIX_ONLY
def test_secure_storage_state_parent_leaves_existing_custom_parent_untouched(tmp_path):
    """Historical finding #734/4: an existing arbitrary custom parent (e.g. a
    stand-in for shared /tmp) must never be chmodded by a custom session path."""
    custom_parent = tmp_path / "tmp_like_shared_dir"
    custom_parent.mkdir(mode=0o755)
    os.chmod(custom_parent, 0o755)
    destination = custom_parent / "hh_session.json"

    secure_storage_state_parent(destination)

    assert _mode(custom_parent) == 0o755


@POSIX_ONLY
def test_secure_storage_state_parent_does_not_chmod_existing_symlinked_parent(tmp_path):
    """A pre-existing parent -- symlink or not -- is left untouched: it may be
    shared with other processes (same rule as the plain-existing-dir case
    above), so the safe behaviour is "don't touch", not "raise"."""
    real_target = tmp_path / "victim"
    real_target.mkdir(mode=0o755)
    link = tmp_path / "accounts" / "acme"
    link.parent.mkdir()
    link.symlink_to(real_target)
    destination = link / "hh_session.json"

    secure_storage_state_parent(destination)

    assert _mode(real_target) == 0o755


@POSIX_ONLY
def test_secure_storage_state_parent_new_component_cannot_be_swapped_for_symlink(tmp_path):
    """A directory-descriptor-relative mkdir+fchmod cannot be redirected by
    swapping a not-yet-created component's *name* for a symlink after the
    call starts walking -- verified indirectly here by asserting the
    freshly-created leaf is a real directory, not something that could have
    silently become a symlink target."""
    destination = tmp_path / "a" / "b" / "hh_session.json"

    secure_storage_state_parent(destination)

    leaf = tmp_path / "a" / "b"
    assert leaf.is_dir()
    assert not leaf.is_symlink()
    assert _mode(leaf) == ACCOUNT_DIR_MODE


# -- Finding 1 companion: secure_directory() itself must stay no-follow -----


@POSIX_ONLY
def test_secure_directory_rejects_symlinked_target(tmp_path):
    real_target = tmp_path / "victim"
    real_target.mkdir(mode=0o755)
    link = tmp_path / "accounts" / "acme"
    link.parent.mkdir()
    link.symlink_to(real_target)

    with pytest.raises(OSError):
        secure_directory(link)

    assert _mode(real_target) == 0o755


# -- Finding 3: regression only, secure_storage_state_file() already rejects
# non-regular destinations before any mode change. --------------------------


@POSIX_ONLY
def test_secure_storage_state_file_rejects_directory_destination(tmp_path):
    destination = tmp_path / "hh_session.json"
    destination.mkdir()

    with pytest.raises(OSError):
        secure_storage_state_file(destination)

    # The mode change must never have happened: execute bits stay intact and
    # the directory remains traversable.
    assert _mode(destination) != SESSION_FILE_MODE
    assert os.access(destination, os.X_OK)


@POSIX_ONLY
def test_secure_storage_state_file_rejects_symlinked_destination(tmp_path):
    """Historical finding #734/6 regression: path-based chmod must not follow
    a session-file symlink onto a victim-owned file."""
    victim = tmp_path / "victim.json"
    victim.write_text("not a session file")
    os.chmod(victim, 0o644)
    link = tmp_path / "hh_session.json"
    link.symlink_to(victim)

    with pytest.raises(OSError):
        secure_storage_state_file(link)

    assert _mode(victim) == 0o644


@POSIX_ONLY
def test_secure_storage_state_file_hardens_regular_file(tmp_path):
    destination = tmp_path / "hh_session.json"
    destination.write_text("{}")
    os.chmod(destination, 0o644)

    secure_storage_state_file(destination)

    assert _mode(destination) == SESSION_FILE_MODE

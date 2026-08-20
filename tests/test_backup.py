import sqlite3
import tarfile
from pathlib import Path

import pytest

from hhru_bot.backup import BackupError, create_backup, inspect_backup, restore_backup

pytestmark = pytest.mark.unit


def _state(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "data" / "config.yaml"
    history = config.parent / "history.db"
    config.parent.joinpath("storage_state").mkdir(parents=True)
    config.write_text(
        "account:\n  storage_state_file: storage_state/session.json\n", encoding="utf-8"
    )
    config.parent.joinpath("storage_state/session.json").write_text("secret", encoding="utf-8")
    with sqlite3.connect(history) as conn:
        conn.execute("create table sample (value text)")
        conn.execute("insert into sample values ('old')")
    return config, history


def test_backup_and_dry_run_restore_do_not_change_state(tmp_path):
    config, history = _state(tmp_path)
    archive = tmp_path / "state.tar.gz"
    create_backup(config, history, archive)
    config.write_text("changed", encoding="utf-8")

    names = restore_backup(archive, config, history)

    assert names == ["config.yaml", "history.db", "storage_state/session.json"]
    assert config.read_text(encoding="utf-8") == "changed"


def test_restore_rejects_path_traversal_and_symlinks(tmp_path):
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("../config.yaml")
        info.size = 0
        tar.addfile(info)
    with pytest.raises(BackupError, match="Небезопасный путь"):
        inspect_backup(archive)


def test_restore_uses_staged_files(tmp_path):
    config, history = _state(tmp_path)
    archive = tmp_path / "state.tar.gz"
    create_backup(config, history, archive)
    config.write_text("changed", encoding="utf-8")

    restore_backup(archive, config, history, dry_run=False)

    assert config.read_text(encoding="utf-8").startswith("account:")
    with sqlite3.connect(history) as conn:
        assert conn.execute("select value from sample").fetchone() == ("old",)


def test_restore_backs_up_surviving_custom_session_when_config_is_missing(tmp_path):
    config = tmp_path / "data" / "config.yaml"
    history = config.parent / "history.db"
    config.parent.mkdir(parents=True)
    custom_session = config.parent / "external" / "custom-session.json"
    custom_session.parent.mkdir(parents=True)
    custom_session.write_text("OLD-BACKUP-TOKEN", encoding="utf-8")
    config.write_text(
        "account:\n  storage_state_file: external/custom-session.json\n", encoding="utf-8"
    )
    with sqlite3.connect(history) as conn:
        conn.execute("create table sample (value text)")
        conn.execute("insert into sample values ('old')")
    archive = tmp_path / "state.tar.gz"
    create_backup(config, history, archive)

    # Disaster-recovery scenario: config.yaml and history.db are both gone,
    # but a newer, unsaved session file survives at the same custom path the
    # restored archive will target. Restore must not silently destroy it.
    config.unlink()
    history.unlink()
    custom_session.write_text("NEWER-UNSAVED-TOKEN-NOT-IN-BACKUP", encoding="utf-8")

    restore_backup(archive, config, history, dry_run=False)

    assert custom_session.read_text(encoding="utf-8") == "OLD-BACKUP-TOKEN"
    rollbacks = list(config.parent.glob(".before-restore-*.tar.gz"))
    assert rollbacks, "restore must snapshot the surviving custom session before overwriting it"
    with tarfile.open(rollbacks[0], "r:*") as tar:
        names = tar.getnames()
    assert "storage_state/custom-session.json" in names


def test_restore_routes_archived_member_by_snapshot_not_by_previous_config_alias(tmp_path):
    # Snapshot state: config points at storage_state/new.json, and
    # storage_state/ also happens to contain an ordinary old.json (e.g. left
    # over from a previous session rotation) that is archived as itself.
    snapshot_config = tmp_path / "snapshot" / "config.yaml"
    snapshot_config.parent.mkdir(parents=True)
    snapshot_history = snapshot_config.parent / "history.db"
    storage_dir = snapshot_config.parent / "storage_state"
    storage_dir.mkdir()
    snapshot_config.write_text(
        "account:\n  storage_state_file: storage_state/new.json\n", encoding="utf-8"
    )
    (storage_dir / "new.json").write_text("NEW-SESSION", encoding="utf-8")
    (storage_dir / "old.json").write_text("ARCHIVED-ORDINARY-OLD-CONTENT", encoding="utf-8")
    with sqlite3.connect(snapshot_history) as conn:
        conn.execute("create table sample (value text)")
        conn.execute("insert into sample values ('old')")
    archive = tmp_path / "state.tar.gz"
    create_backup(snapshot_config, snapshot_history, archive)

    # Live state to restore onto: config points at an EXTERNAL path whose
    # basename collides with the archived storage_state/old.json member.
    config = tmp_path / "data" / "config.yaml"
    history = config.parent / "history.db"
    config.parent.mkdir(parents=True)
    custom_session = config.parent / "custom" / "old.json"
    custom_session.parent.mkdir(parents=True)
    custom_session.write_text("STALE-EXTERNAL-TOKEN", encoding="utf-8")
    config.write_text("account:\n  storage_state_file: custom/old.json\n", encoding="utf-8")

    restore_backup(archive, config, history, dry_run=False)

    # The archived member lands at its own canonical destination, not at the
    # previous live config's unrelated external alias.
    assert (config.parent / "storage_state" / "old.json").read_text(
        encoding="utf-8"
    ) == "ARCHIVED-ORDINARY-OLD-CONTENT"
    # The stale external session (no longer configured by the restored
    # snapshot) is removed, not silently overwritten with unrelated content.
    assert not custom_session.exists()
    rollbacks = list(config.parent.glob(".before-restore-*.tar.gz"))
    assert rollbacks, "the stale external session must be recoverable from a rollback snapshot"
    with tarfile.open(rollbacks[0], "r:*") as tar:
        member = tar.extractfile("storage_state/old.json")
        assert member is not None
        assert member.read().decode("utf-8") == "STALE-EXTERNAL-TOKEN"


def test_restore_maps_canonical_members_to_custom_paths(tmp_path):
    config, history = _state(tmp_path)
    custom_config = config.with_name("custom.yaml")
    custom_history = history.with_name("custom.sqlite")
    config.rename(custom_config)
    history.rename(custom_history)
    archive = tmp_path / "state.tar.gz"

    create_backup(custom_config, custom_history, archive)
    restore_backup(archive, custom_config, custom_history, dry_run=False)

    assert custom_config.read_text(encoding="utf-8").startswith("account:")
    with sqlite3.connect(custom_history) as conn:
        assert conn.execute("select value from sample").fetchone() == ("old",)

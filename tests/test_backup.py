import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from hhru_bot.backup import (
    BackupError,
    _unique_stamped_path,
    create_backup,
    inspect_backup,
    restore_backup,
)

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


def test_restore_rejects_malformed_history_db_with_backup_error(tmp_path):
    # A malformed history.db in the archive (not a valid SQLite file) must
    # surface as BackupError, matching the adjacent code comment ("reject
    # malformed databases") — not a raw sqlite3.DatabaseError leaking out of
    # _sqlite_snapshot's re-materialization step.
    config, history = _state(tmp_path)
    archive = tmp_path / "bad-history.tar"
    with tarfile.open(archive, "w") as tar:
        config_payload = config.read_bytes()
        config_info = tarfile.TarInfo("config.yaml")
        config_info.size = len(config_payload)
        tar.addfile(config_info, io.BytesIO(config_payload))

        garbage = b"not a sqlite database at all"
        history_info = tarfile.TarInfo("history.db")
        history_info.size = len(garbage)
        tar.addfile(history_info, io.BytesIO(garbage))

    with pytest.raises(BackupError, match="[Нн]екорректн"):
        restore_backup(archive, config, history, dry_run=False)


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


def test_two_restores_within_the_same_second_keep_both_rollback_archives(tmp_path, monkeypatch):
    # Rollback filenames used to carry only second-resolution timestamps
    # (datetime.now().strftime("%Y%m%d-%H%M%S")). Two restores completed
    # within the same wall-clock second wrote the identical
    # .before-restore-<stamp>.tar.gz path, and create_backup()'s os.replace
    # silently destroyed the first restore's recovery point. Freeze the
    # clock to exercise exactly that same-second collision.
    import hhru_bot.backup as backup_module

    class _FrozenDatetime(backup_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return backup_module.datetime(2026, 1, 1, 12, 0, 0)

    monkeypatch.setattr(backup_module, "datetime", _FrozenDatetime)

    config, history = _state(tmp_path)
    archive = tmp_path / "state.tar.gz"
    create_backup(config, history, archive)

    restore_backup(archive, config, history, dry_run=False)
    rollbacks_after_first = list(config.parent.glob(".before-restore-*.tar.gz"))
    assert len(rollbacks_after_first) == 1

    restore_backup(archive, config, history, dry_run=False)
    rollbacks_after_second = list(config.parent.glob(".before-restore-*.tar.gz"))

    # Both restores happened at the identical frozen timestamp — the second
    # rollback archive must not overwrite the first.
    assert len(rollbacks_after_second) == 2


def test_unique_stamped_path_avoids_collision_at_identical_timestamp(tmp_path):
    # commands/backup.py's _backup() default --output naming shares this same
    # collision class as the rollback archive: a plain second-resolution
    # timestamp repeats when two calls land in the same wall-clock second (or
    # the same frozen instant in a test), and the CLI's create_backup() call
    # replaces the destination atomically — silently destroying the first
    # backup archive.
    first = _unique_stamped_path(tmp_path, "backup")
    first.write_bytes(b"first archive")

    second = _unique_stamped_path(tmp_path, "backup")

    assert second != first
    # The first archive must still be there and unmodified.
    assert first.read_bytes() == b"first archive"


def test_restore_reports_backup_error_when_extractfile_returns_none(tmp_path, monkeypatch):
    # extractfile() returning None for a member that passed the file/dir
    # type checks should surface as a BackupError, not an AssertionError
    # (stripped under `python -O`) or an uncontrolled AttributeError.
    config, history = _state(tmp_path)
    archive = tmp_path / "state.tar.gz"
    create_backup(config, history, archive)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda self, member: None)

    with pytest.raises(BackupError, match="Не удалось прочитать содержимое"):
        restore_backup(archive, config, history, dry_run=False)


def test_restore_rejects_oversized_archive_member(tmp_path, monkeypatch):
    # restore_backup() must reject a member whose declared TarInfo.size
    # exceeds the cap *before* reading its content into memory — an
    # unbounded `source.read()` on a maliciously large member is an
    # unbounded memory/disk gap in an otherwise defense-in-depth extraction
    # path (path traversal, symlinks and wrong types are already rejected).
    # A real multi-GiB member would make the test itself slow/heavy, so the
    # cap is lowered via monkeypatch and the member is sized just above it —
    # exercising the exact same size-check code path with tiny fixtures.
    monkeypatch.setattr("hhru_bot.backup._MAX_MEMBER_SIZE", 1024)
    config, history = _state(tmp_path)
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as tar:
        payload = b"x" * 2048
        info = tarfile.TarInfo("config.yaml")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with pytest.raises(BackupError, match="превышает допустимый размер"):
        restore_backup(archive, config, history, dry_run=False)


def test_restore_reports_rollback_archive_path(tmp_path):
    # The rollback snapshot created before overwriting anything must be
    # discoverable by the caller, not just written silently to disk — a
    # crash mid-restore should still leave the user a CLI-visible pointer
    # to the archive that can undo it.
    config, history = _state(tmp_path)
    archive = tmp_path / "state.tar.gz"
    create_backup(config, history, archive)

    seen: list[Path] = []
    restore_backup(archive, config, history, dry_run=False, on_rollback=seen.append)

    assert len(seen) == 1
    rollback = seen[0]
    assert rollback.is_file()
    assert rollback.name.startswith(".before-restore-")


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

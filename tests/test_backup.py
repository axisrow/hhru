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

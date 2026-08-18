from __future__ import annotations

import pytest

from hhru_bot.cli import WRITE_COMMANDS, main
from hhru_bot.write_lock import WriteLockBusy, acquire_write_lock

pytestmark = pytest.mark.unit


def test_write_lock_blocks_second_process_descriptor(tmp_path):
    path = tmp_path / ".hhru.lock"
    with acquire_write_lock(path):
        with pytest.raises(WriteLockBusy):
            with acquire_write_lock(path):
                pass


def test_write_lock_can_be_reused_after_release(tmp_path):
    path = tmp_path / ".hhru.lock"
    with acquire_write_lock(path):
        pass
    with acquire_write_lock(path):
        pass


def test_cli_rejects_concurrent_write_command(tmp_path, capsys):
    history = tmp_path / "history.db"
    lock = tmp_path / ".hhru.lock"
    with acquire_write_lock(lock):
        with pytest.raises(SystemExit) as exc:
            main(["--history", str(history), "bump"])
    assert exc.value.code == 1
    assert "другой процесс уже выполняет WRITE-действие" in capsys.readouterr().out


def test_lock_covers_all_hhru_write_commands():
    assert WRITE_COMMANDS == {
        "apply",
        "bump",
        "run",
        "copy-resume",
        "publish-resume",
        "reply-employers",
        "clear-negotiations",
    }

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hhru_bot import cli
from hhru_bot.cli import WRITE_COMMANDS, _is_write_command, _write_lock_path, main
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


def test_lock_collision_reports_owner_from_adjacent_metadata(tmp_path):
    import hhru_bot.write_lock as write_lock

    path = tmp_path / ".hhru.lock"

    with acquire_write_lock(path, command="probe"):
        owner_path = path.with_name(f"{path.name}.owner")
        owner = json.loads(owner_path.read_text())
        assert owner["command"] == "probe"
        with pytest.raises(write_lock.WriteLockBusy) as error:
            with acquire_write_lock(path, command="apply"):
                pass
        assert error.value.owner["command"] == "probe"
        assert error.value.owner["pid"] > 0
        assert error.value.owner["started_at"]


@pytest.mark.skipif(os.name != "nt", reason="Windows import regression test")
def test_cli_import_does_not_require_posix_lock_module():
    env = os.environ.copy()
    src = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH"))))
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import hhru_bot.cli; assert 'fcntl' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_rejects_concurrent_write_command(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
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
        "rename-resume",
        "publish-resume",
        "resume-visibility",
        "edit-experience",
        "about",
        "reply-employers",
        "responses",
        "edit-education",
        "clear-negotiations",
        "delete-education-entry",
        "delete-resume",
        "create-resume",
        "resume-position",
        "resume-sections",
        "resume-pool",
        "edit-skills",
        "edit-languages",
        "settings",
        "reject",
        "review",
        "config",
        "backup",
        "restore",
        "blacklist",
        "update",
        "report-vacancy",
    }


def test_config_read_commands_are_not_write_locked():
    parser = cli.build_parser()
    for argv in (["config"], ["config", "-p"], ["config", "-k", "account"]):
        args = parser.parse_args(argv)
        assert not _is_write_command(args)

    for argv in (["config", "-e"], ["config", "-s", "a", "b"], ["config", "-u", "a"]):
        args = parser.parse_args(argv)
        assert _is_write_command(args)


def test_professional_roles_is_write_locked_only_for_refresh():
    parser = cli.build_parser()

    search = parser.parse_args(["professional-roles", "--query", "разработчик"])
    refresh = parser.parse_args(["professional-roles", "--refresh"])

    assert not _is_write_command(search)
    assert _is_write_command(refresh)


def test_competitors_is_write_locked_only_for_collect():
    parser = cli.build_parser()
    collect = parser.parse_args(["competitors", "collect", "--text", "AI"])
    report = parser.parse_args(["competitors", "report", "--text", "AI"])
    assert _is_write_command(collect)
    assert not _is_write_command(report)


def test_professional_roles_refresh_uses_cache_specific_lock(monkeypatch, tmp_path):
    from hhru_bot import professional_roles

    cache = tmp_path / "cache" / "professional_roles.json"
    monkeypatch.setattr(professional_roles, "DEFAULT_CACHE_PATH", cache)
    args = cli.build_parser().parse_args(["professional-roles", "--refresh"])

    assert _write_lock_path(args) == (cache.parent / ".professional_roles.lock").resolve()


def test_config_write_lock_uses_config_directory(tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(tmp_path / "settings" / "config.yaml"),
            "--history",
            str(tmp_path / "other" / "history.db"),
            "config",
            "-s",
            "a",
            "b",
        ]
    )
    cli._resolve_paths(args)
    assert _write_lock_path(args) == (tmp_path / "settings" / ".hhru.lock").resolve()


@pytest.mark.parametrize("write_config", [False, True])
def test_copy_resume_lock_uses_account_config_directory(tmp_path, write_config):
    parser = cli.build_parser()
    argv = [
        "--config",
        str(tmp_path / "settings" / "config.yaml"),
        "--history",
        str(tmp_path / "other" / "history.db"),
        "copy-resume",
        "--resume",
        "backend",
    ]
    if write_config:
        argv.append("--write-config")

    args = parser.parse_args(argv)
    cli._resolve_paths(args)

    assert _write_lock_path(args) == (tmp_path / "settings" / ".hhru.lock").resolve()


def test_copy_resume_different_history_files_share_account_lock(tmp_path):
    parser = cli.build_parser()
    config = tmp_path / "account" / "config.yaml"
    lock_paths = set()

    for history_name in ("first.db", "isolated-live-test.db"):
        args = parser.parse_args(
            [
                "--config",
                str(config),
                "--history",
                str(tmp_path / "history" / history_name),
                "copy-resume",
                "--resume",
                "backend",
            ]
        )
        cli._resolve_paths(args)
        lock_paths.add(_write_lock_path(args))

    assert lock_paths == {(tmp_path / "account" / ".hhru.lock").resolve()}


def test_resume_pool_different_history_files_share_account_lock(tmp_path):
    """resume-pool делает ту же account-wide reconciliation, что и copy-resume,
    несколько раз подряд (по разу на кластер) — тот же race (#558/a52306c),
    только intra-process вместо inter-process, требует того же lock-root."""
    parser = cli.build_parser()
    config = tmp_path / "account" / "config.yaml"
    lock_paths = set()

    for history_name in ("first.db", "isolated-live-test.db"):
        args = parser.parse_args(
            [
                "--config",
                str(config),
                "--history",
                str(tmp_path / "history" / history_name),
                "resume-pool",
                "--source",
                "backend",
            ]
        )
        cli._resolve_paths(args)
        lock_paths.add(_write_lock_path(args))

    assert lock_paths == {(tmp_path / "account" / ".hhru.lock").resolve()}


def test_account_list_is_read_only_and_bypasses_lock_and_logging(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        cli,
        "setup_logging",
        lambda **kwargs: pytest.fail("account list must not initialize file logging"),
    )
    monkeypatch.setattr(
        cli,
        "acquire_write_lock",
        lambda path: pytest.fail("account list must not acquire the write lock"),
    )

    main(["account", "list"])

    assert "Аккаунтов не найдено" in capsys.readouterr().out
    assert not (tmp_path / "data").exists()

from __future__ import annotations

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
        "edit-education",
        "clear-negotiations",
        "delete-resume",
        "create-resume",
        "resume-position",
        "resume-sections",
        "edit-skills",
        "edit-languages",
        "settings",
        "reject",
        "review",
        "config",
        "backup",
        "restore",
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

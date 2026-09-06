"""Codex Chromium preflight at the side-effect-free CLI boundary (#568)."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from hhru_bot import cli

pytestmark = pytest.mark.unit


def test_browser_command_registry_is_complete() -> None:
    assert cli.BROWSER_COMMANDS == {
        "about",
        "census",
        "adaptive-resume",
        "apply",
        "bump",
        "call-api",
        "clear-negotiations",
        "common",
        "competitors",
        "copy-resume",
        "create-resume",
        "delete-education-entry",
        "delete-resume",
        "edit-education",
        "edit-experience",
        "edit-languages",
        "edit-skills",
        "fill-form",
        "login",
        "login-code",
        "probe",
        "professional-roles",
        "publish-resume",
        "refresh-token",
        "rename-resume",
        "reply-employers",
        "report-vacancy",
        "responses",
        "resume-position",
        "resume-sections",
        "resume-pool",
        "resume-visibility",
        "resume-views",
        "run",
        "search",
        "select-photo",
        "upload-photo",
        "delete-photo",
        "wizard-next",
    }
    for command in cli.BROWSER_COMMANDS - {
        "adaptive-resume",
        "clear-negotiations",
        "competitors",
        "professional-roles",
    }:
        assert cli._requires_browser(Namespace(command=command))


def test_registry_covers_command_modules_that_call_launch_context() -> None:
    commands_dir = Path(__file__).resolve().parents[1] / "src" / "hhru_bot" / "commands"
    direct_browser_commands = {
        path.stem.replace("_", "-")
        for path in commands_dir.glob("*.py")
        if "launch_context" in path.read_text()
    }

    assert direct_browser_commands - {"list-resumes", "whoami"} <= cli.BROWSER_COMMANDS


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (Namespace(command="list-resumes", local=False), True),
        (Namespace(command="list-resumes", local=True), False),
        (Namespace(command="whoami", online=True), True),
        (Namespace(command="whoami", online=False), False),
        (Namespace(command="professional-roles", refresh=False), False),
        (Namespace(command="professional-roles", refresh=True), True),
        (Namespace(command="competitors", competitors_command="collect"), True),
        (Namespace(command="competitors", competitors_command="report"), False),
        (
            Namespace(command="clear-negotiations", account_wide=False, topic="77", dry_run=True),
            False,
        ),
        (
            Namespace(command="clear-negotiations", account_wide=False, topic="77", dry_run=False),
            True,
        ),
        (
            Namespace(command="clear-negotiations", account_wide=True, topic=None, dry_run=True),
            True,
        ),
        (
            Namespace(command="clear-negotiations", account_wide=False, topic=None, dry_run=False),
            False,
        ),
        (Namespace(command="stats"), False),
        (Namespace(command="import-cookies"), False),
    ],
)
def test_conditional_and_local_browser_classification(args: Namespace, expected: bool) -> None:
    assert cli._requires_browser(args) is expected


def test_sandboxed_write_is_rejected_before_any_local_side_effect(
    tmp_path, monkeypatch, capsys
) -> None:
    from hhru_bot.commands import bump

    config = tmp_path / "config.yaml"
    history = tmp_path / "history.db"
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setattr(
        cli,
        "_resolve_paths",
        lambda _args: pytest.fail("sandbox preflight must precede path resolution"),
    )
    monkeypatch.setattr(
        cli,
        "setup_logging",
        lambda **_kwargs: pytest.fail("sandbox preflight must precede logging"),
    )
    monkeypatch.setattr(
        cli,
        "acquire_write_lock",
        lambda *_args, **_kwargs: pytest.fail("sandbox preflight must precede write-lock"),
    )
    monkeypatch.setattr(
        bump,
        "run",
        lambda _args: pytest.fail("sandbox preflight must not invoke the command"),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "--config",
                str(config),
                "--history",
                str(history),
                "bump",
            ]
        )

    assert exc_info.value.code == 1
    error = capsys.readouterr().err
    assert "CODEX_SANDBOX_BROWSER_REQUIRED" in error
    assert "команда 'bump'" in error
    assert "sandbox_permissions=require_escalated" in error
    assert "durable run не начинались" in error
    assert not config.exists()
    assert not history.exists()
    assert not (tmp_path / ".hhru.lock").exists()
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize(
    ("module_name", "argv"),
    [
        ("list_resumes", ["list-resumes", "--local"]),
        ("professional_roles", ["professional-roles", "--query", "разработчик"]),
        ("whoami", ["whoami"]),
    ],
)
def test_local_modes_still_execute_inside_sandbox(module_name, argv, monkeypatch, capsys) -> None:
    import importlib

    command = importlib.import_module(f"hhru_bot.commands.{module_name}")
    calls = []
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    monkeypatch.setattr(command, "run", lambda args: calls.append(args.command))
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    cli.main(argv)

    assert calls == [argv[0]]
    assert "CODEX_SANDBOX_BROWSER_REQUIRED" not in capsys.readouterr().err


def test_empty_sandbox_allows_browser_command_even_with_network_flag(monkeypatch) -> None:
    from hhru_bot.commands import search

    calls = []
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setattr(search, "run", lambda args: calls.append(args.command))
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    cli.main(["search"])

    assert calls == ["search"]

"""Local CLI workflow for confirmed questionnaire templates (#482)."""

from __future__ import annotations

import pytest

from hhru_bot import cli
from hhru_bot.cli import _is_write_command, build_parser, main
from hhru_bot.history import History

pytestmark = pytest.mark.unit


def test_questionnaire_read_and_write_subcommands_are_classified():
    parser = build_parser()
    assert not _is_write_command(parser.parse_args(["questionnaire", "pending"]))
    assert not _is_write_command(parser.parse_args(["questionnaire", "templates"]))
    for action in ("learn", "set", "unset"):
        argv = ["questionnaire", action]
        if action == "set":
            argv += ["location", "--mode", "static", "--answer", "Москва"]
        elif action == "unset":
            argv += ["location"]
        assert _is_write_command(parser.parse_args(argv))


def test_templates_shows_seed_catalog_on_fresh_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    main(["--history", str(tmp_path / "history.db"), "questionnaire", "templates"])

    output = capsys.readouterr().out
    assert "salary" in output
    assert "location" in output
    assert "desired_role" in output
    assert "business_segments" in output


def test_set_and_unset_confirmed_answer(tmp_path, monkeypatch):
    history_path = tmp_path / "history.db"
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    main(
        [
            "--history",
            str(history_path),
            "questionnaire",
            "set",
            "location",
            "--mode",
            "static",
            "--answer",
            "Москва",
        ]
    )
    assert History(history_path).get_questionnaire_answer("location", "") is not None

    main(
        [
            "--history",
            str(history_path),
            "questionnaire",
            "unset",
            "location",
        ]
    )
    assert History(history_path).get_questionnaire_answer("location", "") is None


def test_invalid_set_returns_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: None)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--history",
                str(tmp_path / "history.db"),
                "questionnaire",
                "set",
                "missing_template",
                "--mode",
                "static",
                "--answer",
                "value",
            ]
        )

    assert exc.value.code == 1
    assert "Шаблон не найден" in capsys.readouterr().out

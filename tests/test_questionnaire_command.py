"""Tests for `hhru questionnaire pending/templates/set/unset/learn` (#482).

READ subcommands (pending/templates) never touch the write lock. WRITE
subcommands (set/unset/learn) go through the same shared local lock as
`account create` (issue: "локальные WRITE-команды используют общий lock").
`learn` in a non-interactive/non-TTY run must fail cleanly, never hang
(issue: "Headless/non-TTY: неизвестный вопрос идет в очередь").
"""

from __future__ import annotations

import argparse
import textwrap

import pytest

from hhru_bot import cli
from hhru_bot.commands import questionnaire as questionnaire_cmd
from hhru_bot.history import History

pytestmark = pytest.mark.unit


def _build():
    return cli.build_parser()


def _write_config(tmp_path):
    """One resume, slug 'backend' -> resume_id '11111111' (#319 hash addressing)."""
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            account:
              storage_state_file: data/storage_state/hh_session.json
            resumes:
              - id: backend
                resume_url: "https://hh.ru/resume/11111111"
                search:
                  text: "python developer"
            """
        ),
        encoding="utf-8",
    )
    return path


def test_questionnaire_registered_with_subcommands():
    parser = _build()
    action = next(a for a in parser._subparsers._group_actions if a.dest == "command")
    sub = action.choices["questionnaire"]
    sub_action = next(
        a for a in sub._subparsers._group_actions if a.dest == "questionnaire_command"
    )
    assert set(sub_action.choices) == {"pending", "templates", "learn", "set", "unset"}


@pytest.mark.parametrize(
    "argv",
    [
        ["questionnaire", "pending"],
        ["questionnaire", "templates"],
    ],
)
def test_read_subcommands_are_not_write_locked(argv):
    parser = _build()
    args = parser.parse_args(argv)
    assert not cli._is_write_command(args)


@pytest.mark.parametrize(
    "argv",
    [
        ["questionnaire", "set", "salary", "--mode", "static", "--answer", "300000"],
        ["questionnaire", "unset", "salary"],
        ["questionnaire", "learn"],
    ],
)
def test_write_subcommands_are_write_locked(argv):
    parser = _build()
    args = parser.parse_args(argv)
    assert cli._is_write_command(args)


def test_pending_prints_info_when_empty(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    args = argparse.Namespace(history=str(history_path), resume=None)
    questionnaire_cmd.run_pending(args)
    assert "[INFO]" in capsys.readouterr().out


def test_pending_lists_open_questions(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    history = History(history_path)
    history.enqueue_pending(
        resume_id="backend",
        vacancy_id="1",
        question_text="Готовы к переезду?",
        kind="text",
        options=[],
    )
    args = argparse.Namespace(history=str(history_path), resume=None)
    questionnaire_cmd.run_pending(args)
    out = capsys.readouterr().out
    assert "Готовы к переезду?" in out


def test_pending_resolves_resume_slug_to_hash_resume_id(tmp_path, capsys):
    """Regression: `apply` enqueues pending rows keyed by the numeric
    resume_id (ctx.resume_id), not the config slug -- `--resume backend`
    must resolve the slug the same way before filtering, or the row is
    invisible even though it belongs to this resume.
    """
    config_path = _write_config(tmp_path)
    history_path = tmp_path / "history.db"
    History(history_path).enqueue_pending(
        resume_id="11111111",  # the hash addressed by slug 'backend' above
        vacancy_id="1",
        question_text="Готовы к переезду?",
        kind="text",
        options=[],
    )
    args = argparse.Namespace(history=str(history_path), config=str(config_path), resume="backend")
    questionnaire_cmd.run_pending(args)
    out = capsys.readouterr().out
    assert "Готовы к переезду?" in out


def test_learn_resolves_resume_slug_to_hash_resume_id(tmp_path, capsys, monkeypatch):
    config_path = _write_config(tmp_path)
    history_path = tmp_path / "history.db"
    History(history_path).enqueue_pending(
        resume_id="11111111",
        vacancy_id="1",
        question_text="Готовы к переезду?",
        kind="text",
        options=[],
    )
    monkeypatch.setattr(questionnaire_cmd.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(
        history=str(history_path),
        config=str(config_path),
        resume="backend",
        limit=20,
    )
    result = questionnaire_cmd.run_learn(args)
    # A non-empty, resolved pending queue must reach the TTY-required [FAIL]
    # path -- not silently report "queue is empty" because of a scope miss.
    assert result is True
    assert "[FAIL]" in capsys.readouterr().out


def test_templates_prints_info_when_empty(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    args = argparse.Namespace(history=str(history_path))
    questionnaire_cmd.run_templates(args)
    assert "[INFO]" in capsys.readouterr().out


def test_templates_lists_static_and_contextual(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    history = History(history_path)
    history.upsert_template("salary", mode="static")
    history.upsert_template("motivation", mode="contextual", instruction="Explain briefly")
    args = argparse.Namespace(history=str(history_path))
    questionnaire_cmd.run_templates(args)
    out = capsys.readouterr().out
    assert "salary" in out
    assert "motivation" in out


def test_set_static_creates_template_and_account_answer(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        history=str(history_path),
        config="data/config.yaml",
        template="salary",
        mode="static",
        answer="300000",
        instruction=None,
        example=None,
        resume=None,
    )
    result = questionnaire_cmd.run_set(args)
    assert result is not True  # not a failure
    out = capsys.readouterr().out
    assert "[OK]" in out

    history = History(history_path)
    assert history.get_template("salary") is not None
    assert history.get_template_answers("salary")["account"] == "300000"


def test_set_static_without_answer_fails(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        history=str(history_path),
        config="data/config.yaml",
        template="salary",
        mode="static",
        answer=None,
        instruction=None,
        example=None,
        resume=None,
    )
    result = questionnaire_cmd.run_set(args)
    assert result is True
    assert "[FAIL]" in capsys.readouterr().out


def test_set_contextual_without_instruction_fails(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        history=str(history_path),
        config="data/config.yaml",
        template="motivation",
        mode="contextual",
        answer=None,
        instruction=None,
        example=None,
        resume=None,
    )
    result = questionnaire_cmd.run_set(args)
    assert result is True
    assert "[FAIL]" in capsys.readouterr().out


def test_set_contextual_creates_template_with_examples(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        history=str(history_path),
        config="data/config.yaml",
        template="motivation",
        mode="contextual",
        answer=None,
        instruction="Explain briefly",
        example=["Пример 1", "Пример 2"],
        resume=None,
    )
    questionnaire_cmd.run_set(args)
    template = History(history_path).get_template("motivation")
    assert template.instruction == "Explain briefly"
    assert template.examples == ("Пример 1", "Пример 2")


def test_unset_removes_existing_template(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    History(history_path).upsert_template("salary", mode="static")
    args = argparse.Namespace(history=str(history_path), template="salary")
    result = questionnaire_cmd.run_unset(args)
    assert result is not True
    assert "[OK]" in capsys.readouterr().out
    assert History(history_path).get_template("salary") is None


def test_unset_unknown_template_fails(tmp_path, capsys):
    history_path = tmp_path / "history.db"
    args = argparse.Namespace(history=str(history_path), template="unknown")
    result = questionnaire_cmd.run_unset(args)
    assert result is True
    assert "[FAIL]" in capsys.readouterr().out


def test_learn_non_tty_fails_cleanly_instead_of_hanging(tmp_path, capsys, monkeypatch):
    history_path = tmp_path / "history.db"
    History(history_path).enqueue_pending(
        resume_id="backend", vacancy_id="1", question_text="Q", kind="text", options=[]
    )
    monkeypatch.setattr(questionnaire_cmd.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(history=str(history_path), resume=None, limit=20)
    result = questionnaire_cmd.run_learn(args)
    assert result is True
    assert "[FAIL]" in capsys.readouterr().out


def test_learn_non_tty_with_empty_pending_queue_is_a_no_op(tmp_path, capsys, monkeypatch):
    """Nothing to learn -> succeed without requiring a TTY at all."""
    history_path = tmp_path / "history.db"
    monkeypatch.setattr(questionnaire_cmd.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(history=str(history_path), resume=None, limit=20)
    result = questionnaire_cmd.run_learn(args)
    assert result is not True
    assert "[INFO]" in capsys.readouterr().out

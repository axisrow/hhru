"""Команда robot-mark: вердикт пользователя «робот или человек».

Контракты: ровно одно действие (--robot/--human/--clear); --robot заводит
строку robot-очереди идемпотентно; --human резолвит висящую строку (чат
возвращается в план ответов); --clear снимает вердикт, не трогая очередь.
"""

from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import robot_mark as command
from hhru_bot.history import History

pytestmark = pytest.mark.integration


def _args(tmp_path, **overrides):
    values = dict(
        topic="100000001",
        robot=False,
        human=False,
        clear=False,
        history=str(tmp_path / "h.db"),
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_robot_verdict_creates_queue_row_idempotently(tmp_path, capsys):
    history = History(tmp_path / "h.db")
    command.run(_args(tmp_path, robot=True))
    command.run(_args(tmp_path, robot=True))

    assert history.robot_verdict("100000001") == "robot"
    assert history.is_robot_questionnaire("100000001") is True
    with history._connect() as conn:  # noqa: SLF001 - тест читает таблицу напрямую
        assert conn.execute("SELECT COUNT(*) FROM robot_questionnaires").fetchone()[0] == 1
    assert "вердикт — робот" in capsys.readouterr().out


def test_robot_verdict_reopens_resolved_queue_row(tmp_path, capsys):
    """robot-reply уже ответил (строка резолвнута), пользователь решил «всё-таки
    робот» — строка ре-открывается с reason вердикта, очередь не врёт
    «отвечено» при фактическом гейте по вердикту."""
    history = History(tmp_path / "h.db")
    history.mark_robot_questionnaire(
        "100000001", vacancy_id="200000002", reason="robot_questionnaire"
    )
    history.resolve_robot_questionnaire("100000001", answer="Да")

    command.run(_args(tmp_path, robot=True))

    row = history.robot_questionnaire_row("100000001")
    assert row["reason"] == "user_verdict"
    assert row["resolved_at"] is None
    assert history.is_robot_questionnaire("100000001") is True


def test_human_verdict_resolves_pending_queue_row(tmp_path, capsys):
    """Живой сценарий: эвристика ошибочно положила человека в robot-очередь
    (6 пронумерованных вопросов у живого рекрутера) — вердикт возвращает чат
    в обычный план ответов, иначе чат зависал бы без штатного выхода."""
    history = History(tmp_path / "h.db")
    history.mark_robot_questionnaire(
        "100000001", vacancy_id="200000002", reason="robot_questionnaire"
    )

    command.run(_args(tmp_path, human=True))

    assert history.robot_verdict("100000001") == "human"
    assert history.is_robot_questionnaire("100000001") is False
    row = history.robot_questionnaire_row("100000001")
    assert row["resolved_at"] is not None
    assert "очереди снята" in capsys.readouterr().out


def test_human_verdict_without_queue_row_is_plain_ok(tmp_path, capsys):
    history = History(tmp_path / "h.db")
    command.run(_args(tmp_path, human=True))
    assert history.robot_verdict("100000001") == "human"
    assert "robot-очередь для topic пуста" in capsys.readouterr().out


def test_clear_removes_verdict_without_touching_queue(tmp_path, capsys):
    history = History(tmp_path / "h.db")
    history.mark_robot_questionnaire(
        "100000001", vacancy_id="200000002", reason="robot_questionnaire"
    )
    command.run(_args(tmp_path, robot=True))

    command.run(_args(tmp_path, clear=True))

    assert history.robot_verdict("100000001") is None
    # Очередь живёт своей жизнью: clear возвращает решение эвристикам,
    # но висящую строку не резолвит и не удаляет.
    assert history.is_robot_questionnaire("100000001") is True
    assert "вердикт снят" in capsys.readouterr().out


def test_topic_is_required(tmp_path):
    with pytest.raises(SystemExit) as exc:
        command.run(_args(tmp_path, topic="", robot=True))
    assert exc.value.code == 1


def test_action_flag_is_required(tmp_path):
    with pytest.raises(SystemExit) as exc:
        command.run(_args(tmp_path))
    assert exc.value.code == 1


def test_two_action_flags_fail_closed_on_direct_call(tmp_path):
    """argparse mutually-exclusive защищает CLI; прямой вызов run() с двумя
    флагами обязан отказать, а не молча выбрать первый."""
    with pytest.raises(SystemExit) as exc:
        command.run(_args(tmp_path, robot=True, human=True))
    assert exc.value.code == 1

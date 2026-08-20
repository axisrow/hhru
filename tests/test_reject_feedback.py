from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import reject as reject_cmd
from hhru_bot.history import History

pytestmark = pytest.mark.integration


def test_record_reject_keeps_reason_and_bounded_redacted_diff(tmp_path):
    history = History(tmp_path / "history.db")
    feedback_id = history.record_reject(
        "resume-1",
        "vacancy-1",
        "  не подходит   формат  ",
        generated_letter="Здравствуйте, я Python-разработчик.",
        edited_letter="Здравствуйте, я Python-разработчик. contact@example.com",
    )

    row = history.list_feedback()[0]
    assert row["id"] == feedback_id
    assert row["reason"] == "не подходит формат"
    assert "[redacted-email]" in row["edited_snippet"]
    assert "contact@example.com" not in row["edited_snippet"]
    assert history.list_actions("resume-1", "all", limit=1)[0]["action"] == "reject"


def test_record_reject_rejects_empty_reason(tmp_path):
    history = History(tmp_path / "history.db")
    with pytest.raises(ValueError, match="Причина"):
        history.record_reject("r", "v", "  \n")


def test_reject_command_records_feedback(tmp_path, capsys):
    result = reject_cmd.run(
        argparse.Namespace(
            history=str(tmp_path / "history.db"),
            resume="r",
            vacancy="v",
            reason="не мой стек",
            generated_letter="old",
            edited_letter="new",
        )
    )
    assert result is False
    assert "отклонена" in capsys.readouterr().out

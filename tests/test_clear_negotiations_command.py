from __future__ import annotations

import argparse

import pytest

from hhru_bot.commands import clear_negotiations as command
from hhru_bot.history import History


def _args(**overrides):
    values = dict(
        topic=None,
        vacancy=None,
        resume=None,
        account_wide=False,
        dry_run=False,
        force=False,
        config="unused",
        history="unused",
        headless=True,
        max_pages=5,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_vacancy_force_is_always_rejected():
    with pytest.raises(SystemExit) as exc:
        command.run(_args(vacancy="123", force=True, dry_run=True))
    assert exc.value.code == 1


def test_resume_force_is_plan_only(capsys):
    command.run(_args(resume="work", force=True))
    assert "Только план" in capsys.readouterr().out


def test_topic_without_force_is_rejected_noninteractive(monkeypatch):
    monkeypatch.setattr(command, "confirm_write", lambda *args, **kwargs: False)
    with pytest.raises(SystemExit) as exc:
        command.run(_args(topic="77"))
    assert exc.value.code == 1


def test_successful_withdraw_is_audited(tmp_path, monkeypatch):
    class Response:
        ok = True
        status = 204

    class Request:
        def delete(self, url):
            assert url.endswith("/negotiations/active/77")
            return Response()

    class Page:
        request = Request()

    class Throttle:
        def wait(self, reason):
            pass

    history = History(tmp_path / "history.db")
    command._run_topics(
        _args(force=True), ["77"], page=Page(), history=history, throttle=Throttle()
    )
    with history._connect() as conn:
        row = conn.execute(
            "SELECT resume_id, vacancy_id, action, status FROM actions"
        ).fetchone()
    assert tuple(row) == (command.ACCOUNT_SCOPE, "77", "withdraw", "success")

"""Тесты команды wizard-next (#1010): аудит в history, гейт --allow-auto-publish.

WRITE-hh-ru команда: боевой режим требует --force И --allow-auto-publish (NEXT
на последнем экране hh.ru публикует резюме сам, #900); --dry-run ничего не
нажимает и ничего не пишет в history. Браузер не запускается: launch_context
и функции resume_wizard подменяются, история — реальная SQLite.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.publish_resume as publish_cmd
import hhru_bot.commands.wizard_next as cmd
import hhru_bot.publish_resume
import hhru_bot.resume_wizard as rw
from hhru_bot.history import History
from hhru_bot.publish_resume import PublishResumeResult
from hhru_bot.resume_state import ResumeState
from hhru_bot.resume_wizard import WizardAdvanceResult

pytestmark = pytest.mark.integration

RESUME_ID = "a" * 38


def _fake_config(tmp_path):
    resume = SimpleNamespace(id="python", resume_id=RESUME_ID)

    def get_resume(rid):
        if rid != "python":
            from hhru_bot.config import ConfigError

            raise ConfigError(f"Резюме '{rid}' не найдено в конфиге.")
        return resume

    return SimpleNamespace(
        get_resume=get_resume,
        storage_state_file=tmp_path / "session.json",
        user_agent=None,
    )


def _args(tmp_path, **overrides):
    base = {
        "config": "unused.yaml",
        "history": str(tmp_path / "h.db"),
        "headless": True,
        "resume": "python",
        "screen": None,
        "dry_run": False,
        "force": False,
        "allow_auto_publish": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Подменяет config/браузер/resume_wizard; state_queue задаёт чтения состояния."""
    state = SimpleNamespace(
        # первое чтение — до клика, второе — контрольное после него
        state_queue=[
            ResumeState(status="not_finished", next_incomplete_screen_id="educations"),
            ResumeState(status="not_finished", next_incomplete_screen_id="keyskills"),
        ],
        result=WizardAdvanceResult("educations", True, "экран «educations» подтверждён", acted=True),
        submit_calls=0,
        inspect_calls=0,
        launched=0,
    )

    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _fake_config(tmp_path))

    @contextmanager
    def fake_launch(*a, **kw):
        state.launched += 1
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fake_launch)

    def fake_read_state(page, resume_id):
        if not state.state_queue:
            raise AssertionError("неожиданное чтение состояния")
        return state.state_queue.pop(0)

    def fake_submit(page, resume, target, *, before_click=None):
        state.submit_calls += 1
        if state.result.success or state.result.uncertain:
            before_click()
        return state.result

    def fake_inspect(page, resume_id, target):
        state.inspect_calls += 1
        return "Сохранить и продолжить"

    monkeypatch.setattr(rw, "read_resume_state", fake_read_state)
    monkeypatch.setattr(rw, "submit_wizard_screen", fake_submit)
    monkeypatch.setattr(rw, "inspect_wizard_screen", fake_inspect)
    return state


def test_run_without_force_exits_before_browser(env, tmp_path):
    with pytest.raises(SystemExit):
        cmd.run(_args(tmp_path, force=False, allow_auto_publish=True))
    assert env.launched == 0 and env.submit_calls == 0


def test_run_without_allow_auto_publish_refuses_auto_publish_gate(env, capsys, tmp_path):
    """#900: каждый NEXT визарда может оказаться последним экраном — гейт обязателен."""
    with pytest.raises(SystemExit):
        cmd.run(_args(tmp_path, force=True, allow_auto_publish=False))
    out = capsys.readouterr().out
    assert env.launched == 0 and env.submit_calls == 0
    assert "--allow-auto-publish" in out


def test_run_success_records_history_and_next_screen(env, capsys, tmp_path):
    assert cmd.run(_args(tmp_path, force=True, allow_auto_publish=True)) is False
    out = capsys.readouterr().out
    assert "[OK] Экран «educations» подтверждён" in out
    assert "Следующий незавершённый экран: keyskills" in out
    assert "wizard-next --resume python --allow-auto-publish --force" in out
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "wizard_next") == 1
    run = h.command_runs()[-1]
    assert (run["command"], run["status"], run["attempted"], run["success"], run["failed"]) == (
        "wizard-next",
        "completed",
        1,
        1,
        0,
    )


def test_run_auto_publish_outcome_is_reported(env, capsys, tmp_path):
    env.state_queue[1] = ResumeState(status="finished", is_searchable=True)
    assert cmd.run(_args(tmp_path, force=True, allow_auto_publish=True)) is False
    out = capsys.readouterr().out
    assert "автопубликация #900" in out
    assert "list-resumes" in out


def test_run_uncertain_result_records_and_blocks_retry(env, capsys, tmp_path):
    env.result = WizardAdvanceResult(
        "educations", False, "переход с экрана не подтверждён: timeout", acted=True, uncertain=True
    )
    assert cmd.run(_args(tmp_path, force=True, allow_auto_publish=True)) is True
    assert "[FAIL] (uncertain)" in capsys.readouterr().out
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "wizard_next") == 1

    # повтор после неразрешённого uncertain — отказ до браузера
    with pytest.raises(SystemExit):
        cmd.run(_args(tmp_path, force=True, allow_auto_publish=True))
    assert env.launched == 1  # второй запуск браузер не открывал
    assert env.submit_calls == 1


def test_run_plain_failure_is_not_recorded(env, capsys, tmp_path):
    env.result = WizardAdvanceResult(
        "educations", False, "NEXT не гидратирован за 30с — клик не отправлялся (мутации нет)"
    )
    assert cmd.run(_args(tmp_path, force=True, allow_auto_publish=True)) is True
    assert "[FAIL]" in capsys.readouterr().out
    assert "uncertain" not in capsys.readouterr().out
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "wizard_next") == 0


def test_run_dry_run_opens_screen_without_click_or_history(env, capsys, tmp_path):
    args = _args(tmp_path, dry_run=True)
    assert cmd.run(args) is False
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert env.inspect_calls == 1 and env.submit_calls == 0
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "wizard_next") == 0


def test_run_resolve_refusal_fails_without_attempt(env, capsys, tmp_path):
    env.state_queue = [ResumeState(status="finished", is_searchable=True)]
    assert cmd.run(_args(tmp_path, force=True, allow_auto_publish=True)) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "уже опубликовано" in out
    assert env.submit_calls == 0
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "wizard_next") == 0
    run = h.command_runs()[-1]
    assert (run["status"], run["attempted"]) == ("failed", 0)


def test_publish_resume_guidance_points_to_wizard_next(env, capsys, tmp_path, monkeypatch):
    """#1010: publish-resume для wizard-экранов советует wizard-next, а не ручные клики."""
    state = SimpleNamespace(
        result=PublishResumeResult(
            "python",
            False,
            "незавершённый шаг nextIncompleteScreenId=educations; клик запрещён",
            "not_finished",
            False,
            False,
            "educations",
        ),
        exc=None,
    )

    @contextmanager
    def fake_launch(*a, **kw):
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fake_launch)

    def fake_publish(page, resume, dry_run, *, before_click=None):
        return state.result

    monkeypatch.setattr(hhru_bot.publish_resume, "publish_resume_on_hh", fake_publish)

    args = argparse.Namespace(
        config="unused.yaml",
        history=str(tmp_path / "h.db"),
        headless=True,
        resume="python",
        dry_run=True,
        force=False,
    )
    assert publish_cmd.run(args) is True
    out = capsys.readouterr().out
    assert "wizard-next --resume python --screen educations --allow-auto-publish --force" in out

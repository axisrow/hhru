"""Тесты команды publish-resume (#219): аудит в history, обработка NotAuthenticated.

WRITE-hh-ru команда: боевой режим требует --force; --dry-run ничего не нажимает
и ничего не пишет в history. Браузер здесь не запускается: launch_context и
publish_resume_on_hh подменяются, история — реальная SQLite во временном файле.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.publish_resume as cmd
import hhru_bot.publish_resume
from hhru_bot.history import History
from hhru_bot.publish_resume import PublishResumeResult
from hhru_bot.responses import NotAuthenticated

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
        "dry_run": False,
        "force": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Подменяет config/браузер; result/exc задают исход браузерного шага."""
    state = SimpleNamespace(
        result=PublishResumeResult("python", True, "опубликовано", "finished", True), exc=None
    )

    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _fake_config(tmp_path))

    @contextmanager
    def fake_launch(*a, **kw):
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fake_launch)

    def fake_publish(page, resume, dry_run, *, before_click=None):
        if state.exc is not None:
            raise state.exc
        if not dry_run and (state.result.success or state.result.uncertain):
            before_click()
        return state.result

    monkeypatch.setattr(hhru_bot.publish_resume, "publish_resume_on_hh", fake_publish)
    return state


def test_run_success_records_success_in_history(env, capsys, tmp_path):
    cmd.run(_args(tmp_path, force=True))
    out = capsys.readouterr().out
    assert "[OK] Резюме python опубликовано" in out
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "publish_resume") == 1
    run = h.command_runs()[-1]
    assert (run["command"], run["status"], run["attempted"], run["success"], run["failed"]) == (
        "publish-resume",
        "completed",
        1,
        1,
        0,
    )


def test_run_uncertain_result_records_uncertain_and_fails(env, capsys, tmp_path):
    env.result = PublishResumeResult(
        "python", False, "ошибка клика; результат не подтверждён: boom", uncertain=True
    )
    assert cmd.run(_args(tmp_path, force=True)) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "uncertain" in out
    # uncertain расходует дневной лимит/кулдаун — как success (#176/#207).
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "publish_resume") == 1


def test_run_refuses_retry_after_unresolved_uncertain_without_touching_browser(
    env, capsys, tmp_path
):
    # Codex-раунд 2 (#219): uncertain писался в history, но следующий запуск
    # никак его не учитывал и мог дойти до нового клика на основе свежего
    # (возможно устаревшего сразу после мутации) live-статуса резюме.
    # Fail-closed: неразрешённый uncertain блокирует live-клик до ручной
    # проверки, браузер вообще не запускается.
    h = History(tmp_path / "h.db")
    h.record_action(RESUME_ID, RESUME_ID, "publish_resume", "uncertain", "прошлая попытка")
    browser_called = False

    @contextmanager
    def fail_if_called(*a, **kw):
        nonlocal browser_called
        browser_called = True
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    import hhru_bot.browser as browser_mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(browser_mod, "launch_context", fail_if_called)
        with pytest.raises(SystemExit):
            cmd.run(_args(tmp_path, force=True))
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "не подтверждена" in out
    assert browser_called is False


def test_run_still_refuses_after_intervening_failed_row(env, capsys, tmp_path):
    # /review-раунд 3 (#219): last_action_status читал только САМУЮ СВЕЖУЮ
    # запись — промежуточный failed (например NotAuthenticated при повторной
    # попытке, случившийся ДО клика) стирал бы блокировку от старого
    # неразрешённого uncertain, хотя реальная публикация так и осталась
    # неподтверждённой. has_unresolved_uncertain смотрит на всю историю
    # после последнего success, а не только на последнюю строку.
    h = History(tmp_path / "h.db")
    h.record_action(RESUME_ID, RESUME_ID, "publish_resume", "uncertain", "прошлая попытка")
    h.record_action(RESUME_ID, RESUME_ID, "publish_resume", "failed", "сессия истекла")
    browser_called = False

    @contextmanager
    def fail_if_called(*a, **kw):
        nonlocal browser_called
        browser_called = True
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    import hhru_bot.browser as browser_mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(browser_mod, "launch_context", fail_if_called)
        with pytest.raises(SystemExit):
            cmd.run(_args(tmp_path, force=True))
    assert browser_called is False


def test_run_allows_retry_after_resolved_success(env, capsys, tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action(RESUME_ID, RESUME_ID, "publish_resume", "uncertain", "прошлая попытка")
    h.record_action(RESUME_ID, RESUME_ID, "publish_resume", "success", "опубликовано вручную")
    cmd.run(_args(tmp_path, force=True))
    out = capsys.readouterr().out
    assert "[OK]" in out


def test_run_dry_run_bypasses_uncertain_guard(env, capsys, tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action(RESUME_ID, RESUME_ID, "publish_resume", "uncertain", "прошлая попытка")
    env.result = PublishResumeResult("python", True, "dry-run; кнопка не нажата", "not_finished")
    cmd.run(_args(tmp_path, dry_run=True))
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out


def test_run_plain_failure_is_not_recorded_or_counted(env, capsys, tmp_path):
    env.result = PublishResumeResult("python", False, "кнопка не найдена")
    assert cmd.run(_args(tmp_path, force=True)) is True
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "publish_resume") == 0
    with h._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_run_not_authenticated_is_not_recorded_and_exits(env, capsys, tmp_path):
    env.exc = NotAuthenticated("сессия истекла")
    assert cmd.run(_args(tmp_path, force=True)) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "Сессия недействительна" in out
    # Никакого клика не было вовсе — сессия отвергнута до захода на страницу
    # резюме, поэтому pre-click отказ не оставляет строку в actions.
    with History(tmp_path / "h.db")._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_pre_click_launch_failure_leaves_no_uncertain_marker(env, tmp_path, monkeypatch):
    def fail_launch(*_args, **_kwargs):
        raise RuntimeError("transient launch failure")

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fail_launch)

    with pytest.raises(RuntimeError, match="transient launch failure"):
        cmd.run(_args(tmp_path, force=True))

    history = History(tmp_path / "h.db")
    assert not history.has_unresolved_uncertain(RESUME_ID, "publish_resume")
    assert history.command_runs()[-1]["attempted"] == 0


def test_run_dry_run_writes_nothing_to_history(env, capsys, tmp_path):
    env.result = PublishResumeResult("python", True, "dry-run; кнопка не нажата", "not_finished")
    cmd.run(_args(tmp_path, dry_run=True))
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    h = History(tmp_path / "h.db")
    assert h.count_today(RESUME_ID, "publish_resume") == 0


def test_run_without_force_exits_before_browser(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _fake_config(tmp_path))
    with pytest.raises(SystemExit):
        cmd.run(_args(tmp_path, force=False))
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "--force" in out

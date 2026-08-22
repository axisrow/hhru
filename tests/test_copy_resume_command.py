"""Тесты команды copy-resume (#116): контракт подтверждения, вывод, аудит.

WRITE-hh-ru команда: боевой режим требует --force или интерактивного prompt
в TTY; в неинтерактивном запуске без --force — отказ с exit 1 (единый контракт
§1 cli-spec). Браузер здесь не запускается: launch_context и copy_resume_on_hh
подменяются, история — реальная SQLite во временном файле.
"""

from __future__ import annotations

import argparse
import signal
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.copy_resume as cmd
import hhru_bot.copy_resume
from hhru_bot.copy_resume import CopyResumeResult
from hhru_bot.history import History

pytestmark = pytest.mark.integration

OLD_ID = "a" * 38
NEW_ID = "b" * 38


# --- Чистая логика: контракт подтверждения ---


def test_confirm_force_bypasses_prompt():
    assert cmd.confirm_write(True, prompt="?", isatty_fn=lambda: False) is True


def test_confirm_non_tty_refuses():
    assert (
        cmd.confirm_write(
            False,
            prompt="?",
            isatty_fn=lambda: False,
            input_fn=lambda _: pytest.fail("prompt в неинтерактивном режиме"),
        )
        is False
    )


@pytest.mark.parametrize("answer,expected", [("y", True), ("yes", True), ("", False), ("n", False)])
def test_confirm_tty_prompt(answer, expected):
    assert (
        cmd.confirm_write(False, prompt="?", isatty_fn=lambda: True, input_fn=lambda _: answer)
        is expected
    )


# --- run(): оркестрация с подменёнными браузером и конфигом ---


def _fake_config(tmp_path):
    resume = SimpleNamespace(id="backend", resume_id=OLD_ID)

    def get_resume(rid):
        if rid != "backend":
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
        "resume": "backend",
        "dry_run": False,
        "force": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Подменяет config/браузер; result задаёт исход браузерного шага."""
    state = SimpleNamespace(result=CopyResumeResult("backend", True, NEW_ID), calls=[])

    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _fake_config(tmp_path))

    @contextmanager
    def fake_launch(*a, **kw):
        yield SimpleNamespace(new_page=lambda: SimpleNamespace())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fake_launch)

    def fake_copy(page, resume, dry_run, *, before_click=None):
        state.calls.append((resume.id, dry_run))
        if not dry_run and (state.result.success or state.result.uncertain):
            before_click()
        return state.result

    monkeypatch.setattr(hhru_bot.copy_resume, "copy_resume_on_hh", fake_copy)
    return state


def test_run_success_prints_ok_and_yaml_snippet(env, capsys, tmp_path):
    cmd.run(_args(tmp_path, force=True))
    out = capsys.readouterr().out
    assert f"[OK] Резюме backend скопировано. Новый resume_id: {NEW_ID}" in out
    assert "config.yaml" in out
    assert f"https://hh.ru/resume/{NEW_ID}" in out
    # Аудит в actions.
    h = History(tmp_path / "h.db")
    assert h.count_today(OLD_ID, "copy_resume") == 1
    run = h.command_runs()[-1]
    assert (run["command"], run["status"], run["attempted"], run["success"], run["failed"]) == (
        "copy-resume",
        "completed",
        1,
        1,
        0,
    )


def test_run_without_force_non_tty_exits_1(env, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert env.calls == []  # до браузера не дошли


def test_run_dry_run_needs_no_confirmation(env, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    env.result = CopyResumeResult("backend", True, "", reason="dry-run")
    cmd.run(_args(tmp_path, dry_run=True))
    out = capsys.readouterr().out
    assert "[DRY-RUN] Копирование резюме backend" in out
    assert OLD_ID in out
    assert "[INFO] Ничего не отправлено." in out
    assert env.calls == [("backend", True)]
    # dry_run не считается успехом в count_today (только status='success').
    h = History(tmp_path / "h.db")
    assert h.count_today(OLD_ID, "copy_resume") == 0


def test_run_browser_failure_exits_1(env, capsys, tmp_path):
    reason = "profile_stalled: профиль hh.ru перестал прогружаться; копия не создавалась"
    env.result = CopyResumeResult("backend", False, reason=reason)
    assert cmd.run(_args(tmp_path, force=True)) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert reason in out
    h = History(tmp_path / "h.db")
    assert h.count_today(OLD_ID, "copy_resume") == 0  # failed != success
    with h._connect() as conn:
        row = conn.execute(
            "SELECT status, reason FROM actions WHERE resume_id = ? AND action = 'copy_resume'",
            (OLD_ID,),
        ).fetchone()
    assert row is None
    run = h.command_runs()[-1]
    assert run["attempted"] == run["failed"] == run["uncertain"] == 0


def test_run_same_resume_id_fails_closed(env, capsys, tmp_path):
    # Страховка fail-closed в команде: браузерный шаг вернул success с исходным id.
    env.result = CopyResumeResult("backend", True, OLD_ID)
    assert cmd.run(_args(tmp_path, force=True)) is True
    assert "[FAIL]" in capsys.readouterr().out


def test_run_unknown_resume_exits_1(env, capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path, resume="nope", force=True))
    assert exc.value.code == 1
    assert "[FAIL]" in capsys.readouterr().out
    assert env.calls == []


def test_run_repeat_today_warns(env, capsys, tmp_path):
    h = History(tmp_path / "h.db")
    h.record_action(OLD_ID, OLD_ID, "copy_resume", "success", "new_resume_id=x")
    cmd.run(_args(tmp_path, force=True))
    out = capsys.readouterr().out
    assert "[INFO] Уже копировали backend сегодня" in out


def test_run_browser_exception_still_records_audit_then_reraises(env, tmp_path, monkeypatch):
    # Клик по «Дублировать» уже мог уйти на hh.ru раньше, чем упало исключение
    # (например, goto_hh при diff-fallback исчерпал ретраи) — не должны молчать
    # локально о попытке: пишем uncertain в actions ДО того, как исключение улетит
    # дальше (#132 review: без этого возможна копия на hh.ru без локальной записи).
    def raising_copy(page, resume, dry_run, *, before_click):
        env.calls.append((resume.id, dry_run))
        before_click()
        raise RuntimeError("goto_hh исчерпал ретраи")

    monkeypatch.setattr(hhru_bot.copy_resume, "copy_resume_on_hh", raising_copy)

    with pytest.raises(RuntimeError):
        cmd.run(_args(tmp_path, force=True))

    h = History(tmp_path / "h.db")
    assert h.count_today(OLD_ID, "copy_resume") == 1  # uncertain расходует лимит fail-closed
    with h._connect() as conn:
        row = conn.execute(
            "SELECT status, reason FROM actions WHERE resume_id = ? AND action = 'copy_resume'",
            (OLD_ID,),
        ).fetchone()
    assert row["status"] == "uncertain"
    assert "исключение после точки невозврата" in row["reason"]


def test_pre_click_launch_failure_leaves_no_uncertain_marker(env, tmp_path, monkeypatch):
    def fail_launch(*_args, **_kwargs):
        raise RuntimeError("transient launch failure")

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fail_launch)

    with pytest.raises(RuntimeError, match="transient launch failure"):
        cmd.run(_args(tmp_path, force=True))

    history = History(tmp_path / "h.db")
    assert not history.has_unresolved_uncertain(OLD_ID, "copy_resume")
    assert history.command_runs()[-1]["attempted"] == 0


def test_run_uncertain_blocks_subsequent_copy(env, tmp_path, monkeypatch, capsys):
    # Regression test for Codex finding: uncertain copy results must block retries.
    # Если есть unresolved uncertain запись, следующий run должен fail с explicit error,
    # а не позволять повторный клик который создаст дубликат резюме на hh.ru.
    h = History(tmp_path / "h.db")
    # Сначала записываем uncertain (как будто предыдущий клик мог уйти)
    h.record_action(
        OLD_ID, OLD_ID, "copy_resume", "uncertain", "состояние после WRITE-клика не подтверждено"
    )
    # Пытаемся запустить снова — должен быть rejected
    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path, force=True))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "не подтверждено (uncertain)" in out
    assert env.calls == []  # до браузера не дошло


def test_sigterm_after_clone_click_leaves_unresolved_uncertain_marker(env, tmp_path, monkeypatch):
    """Codex cycle-review PR #470 (round 2): a SIGTERM/KeyboardInterrupt
    delivered right after copy_resume_on_hh's clone click must not let a
    blind retry create a duplicate. ``except Exception`` cannot catch a
    signal-raised ``BaseException`` (KeyboardInterrupt/SignalTermination),
    so today no uncertain actions row is written when the interrupt lands
    after the click already fired.
    """

    def raising_copy(page, resume, dry_run, *, before_click):  # noqa: ANN001, ARG001
        env.calls.append((resume.id, dry_run))
        before_click()
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(hhru_bot.copy_resume, "copy_resume_on_hh", raising_copy)

    cmd.run(_args(tmp_path, force=True))

    h = History(tmp_path / "h.db")
    assert h.has_unresolved_uncertain(OLD_ID, "copy_resume"), (
        "a SIGTERM after the clone click must leave an unresolved uncertain "
        "actions marker, or a blind retry can create a duplicate resume"
    )

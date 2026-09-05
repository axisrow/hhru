"""Безопасный контракт create-resume: dry-run, подтверждение и YAML-вывод."""

from __future__ import annotations

import argparse
import signal
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.create_resume as cmd
import hhru_bot.create_resume
from hhru_bot.catalog_preflight import PreflightOutcome
from hhru_bot.create_resume import CreateResumeResult
from hhru_bot.history import History

pytestmark = pytest.mark.integration
NEW_ID = "b" * 38


def _args(tmp_path, **overrides):
    values = dict(
        config="unused.yaml",
        history=str(tmp_path / "history.db"),
        headless=True,
        area="it",
        title="Backend developer",
        force=False,
        dry_run=False,
        allow_unresolved_area=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _: SimpleNamespace(storage_state_file=tmp_path / "session.json", user_agent=None),
    )

    @contextmanager
    def launch(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", launch)
    state = SimpleNamespace(result=CreateResumeResult(True, NEW_ID, "черновик создан"), calls=[])

    # #950: pre-flight сверка area с live-каталогом — на двойнике проход без
    # предупреждения; конкретные refusal-сценарии — в тестах ниже и в
    # test_catalog_preflight.py.
    monkeypatch.setattr(
        "hhru_bot.catalog_preflight.preflight_area",
        lambda page, area, *, allow_unresolved_area=False: PreflightOutcome(True, ""),
    )

    def create(page, *, area, title, dry_run, before_click=None, allow_unresolved_area=False):
        state.calls.append((area, title, dry_run))
        if not dry_run and (state.result.success or state.result.uncertain):
            before_click()
        return state.result

    monkeypatch.setattr(hhru_bot.create_resume, "create_resume_on_hh", create)
    return state


def test_dry_run_is_default_and_does_not_prompt(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    cmd.run(_args(tmp_path))
    output = capsys.readouterr().out
    assert "[DRY-RUN]" in output
    assert env.calls == [("it", "Backend developer", True)]


def test_preflight_refusal_blocks_wizard_in_live_run(env, tmp_path, capsys, monkeypatch):
    """#950: отказ по area наступает до входа в визард — create не вызывается."""
    monkeypatch.setattr(
        "hhru_bot.catalog_preflight.preflight_area",
        lambda page, area, *, allow_unresolved_area=False: PreflightOutcome(
            False,
            "профессия «Хирург» не найдена в live-каталоге; ближайшие доступные "
            "листы: Врач. Повторите с точным именем листа.",
        ),
    )

    assert cmd.run(_args(tmp_path, force=True)) is True

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "Хирург" in out
    assert "Врач" in out
    assert env.calls == []


def test_preflight_refusal_blocks_dry_run_before_wizard(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "hhru_bot.catalog_preflight.preflight_area",
        lambda page, area, *, allow_unresolved_area=False: PreflightOutcome(
            False, "профессия не найдена в live-каталоге"
        ),
    )

    assert cmd.run(_args(tmp_path, dry_run=True)) is True

    assert "[FAIL]" in capsys.readouterr().out
    assert env.calls == []


def test_preflight_allow_mode_passes_with_warning(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "hhru_bot.catalog_preflight.preflight_area",
        lambda page, area, *, allow_unresolved_area=False: PreflightOutcome(
            True, "профессия не найдена; будет выбрана роль-плейсхолдер «Другое» (id 40)."
        ),
    )

    assert cmd.run(_args(tmp_path, force=True, allow_unresolved_area=True)) is False

    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "«Другое» (id 40)" in out
    assert env.calls == [("it", "Backend developer", False)]


def test_force_prints_yaml_but_does_not_modify_config(env, tmp_path, capsys):
    cmd.run(_args(tmp_path, force=True))
    output = capsys.readouterr().out
    assert f"Новый resume_id: {NEW_ID}" in output
    assert f"https://hh.ru/resume/{NEW_ID}" in output
    assert not (tmp_path / "config.yaml").exists()
    run = History(tmp_path / "history.db").command_runs()[-1]
    assert (run["command"], run["status"], run["attempted"], run["success"], run["failed"]) == (
        "create-resume",
        "completed",
        1,
        1,
        0,
    )


def test_dry_run_wins_when_force_is_also_present(env, tmp_path, capsys):
    cmd.run(_args(tmp_path, force=True, dry_run=True))
    assert "[DRY-RUN]" in capsys.readouterr().out
    assert env.calls == [("it", "Backend developer", True)]


def test_no_force_is_dry_run_even_in_non_tty(env, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    cmd.run(_args(tmp_path, force=False, dry_run=False))
    assert "[DRY-RUN]" in capsys.readouterr().out
    assert env.calls == [("it", "Backend developer", True)]


def test_sigterm_after_creation_leaves_unresolved_uncertain_marker(env, tmp_path, monkeypatch):
    """Codex cycle-review PR #470: a SIGTERM/KeyboardInterrupt delivered after
    ``create_resume_on_hh`` has already created the resume on hh.ru must not
    let a blind retry create a duplicate. ``run_supervised_command``'s
    ``command_runs`` ledger row (status ``interrupted``) is a diagnostic
    record, not the dedup barrier -- that's ``actions``, checked via
    ``has_unresolved_uncertain`` the same way publish-resume/copy-resume
    already do. ``except Exception`` inside ``create_resume.py::_body``
    cannot catch a signal-raised ``BaseException``, so today no ``actions``
    row is written at all when the interrupt lands after a successful
    external creation -- this test pins that gap red until fixed.
    """

    def create(page, *, area, title, dry_run, before_click):  # noqa: ANN001, ARG001
        # Simulate hh.ru having already created the resume, then the process
        # getting SIGTERM'd before the command can record anything.
        before_click()
        signal.raise_signal(signal.SIGTERM)
        return CreateResumeResult(True, NEW_ID, "черновик создан")

    monkeypatch.setattr(hhru_bot.create_resume, "create_resume_on_hh", create)

    cmd.run(_args(tmp_path, force=True))

    history = History(tmp_path / "history.db")
    assert history.has_unresolved_uncertain("account", "create_resume"), (
        "a SIGTERM after hh.ru already created the resume must leave an "
        "unresolved uncertain actions marker, or a blind retry can create "
        "a duplicate resume"
    )


def test_pre_click_launch_failure_leaves_no_uncertain_marker(env, tmp_path, monkeypatch):
    def fail_launch(*_args, **_kwargs):
        raise RuntimeError("transient launch failure")

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fail_launch)

    with pytest.raises(RuntimeError, match="transient launch failure"):
        cmd.run(_args(tmp_path, force=True))

    history = History(tmp_path / "history.db")
    assert not history.has_unresolved_uncertain("account", "create_resume")
    assert history.command_runs()[-1]["attempted"] == 0


def test_unresolved_uncertain_blocks_retry(env, tmp_path, capsys):
    """The guard side of the same #464 fix: once an uncertain marker exists
    (e.g. from the SIGTERM scenario above), a plain retry must refuse rather
    than silently attempt a second creation -- mirrors publish-resume/
    copy-resume's existing ``has_unresolved_uncertain`` guard.
    """
    history = History(tmp_path / "history.db")
    history.record_action("account", "account", "create_resume", "uncertain", "клик мог уйти")

    with pytest.raises(SystemExit):
        cmd.run(_args(tmp_path, force=True))

    output = capsys.readouterr().out
    assert "[FAIL]" in output
    assert "uncertain" in output
    # No browser call was attempted -- the guard fires before _body runs.
    assert env.calls == []


# --- #978: вердикты статусной модели в выводе команды ------------------------


def _force_output(env, tmp_path, capsys, monkeypatch, readiness, **verdict_kwargs):
    """Прогон команды с замоканным readback: вердикт целиком в reason
    (единый каталог draft_verdict_result), CLI выбирает только префикс."""
    import hhru_bot.commands.create_resume as command_module
    from hhru_bot.create_resume import draft_verdict_result

    monkeypatch.setattr(command_module, "apply_draft_readback", lambda page, result: result)
    env.result = draft_verdict_result(
        readiness,
        verdict_kwargs.get("next_incomplete_screen_id"),
        verdict_kwargs.get("detail", ""),
        new_resume_id=NEW_ID,
        placeholder_role=verdict_kwargs.get("placeholder_role", False),
    )
    cmd.run(_args(tmp_path, force=True))
    return capsys.readouterr().out


def test_draft_started_verdict_is_printed(env, tmp_path, capsys, monkeypatch):
    out = _force_output(
        env, tmp_path, capsys, monkeypatch, "draft_started", next_incomplete_screen_id="common"
    )
    assert "[OK] Черновик начат" in out
    assert "nextIncompleteScreenId=common" in out
    assert "publish-resume откажет" in out
    assert f"Новый resume_id: {NEW_ID}" in out


def test_ready_to_publish_verdict_is_printed(env, tmp_path, capsys, monkeypatch):
    out = _force_output(env, tmp_path, capsys, monkeypatch, "ready_to_publish")
    assert "[OK] Готово к публикации" in out
    assert "Черновик начат" not in out


def test_already_published_verdict_is_printed(env, tmp_path, capsys, monkeypatch):
    out = _force_output(env, tmp_path, capsys, monkeypatch, "already_published")
    assert "уже опубликовано" in out
    assert "Готово к публикации:" not in out


def test_unknown_readback_warns_with_failure_detail(env, tmp_path, capsys, monkeypatch):
    """Ревью PR #980: [WARN] печатает result.reason ЦЕЛИКОМ — деталь readback
    (почему именно не удался) доходит до терминала, а не только в history."""
    out = _force_output(env, tmp_path, capsys, monkeypatch, "unknown", detail="timeout навигации")
    assert "[WARN]" in out
    assert "timeout навигации" in out
    assert "не подтверждена" in out
    assert "Готово к публикации" not in out


def test_placeholder_warning_survives_inside_verdict_reason(env, tmp_path, capsys, monkeypatch):
    out = _force_output(
        env,
        tmp_path,
        capsys,
        monkeypatch,
        "draft_started",
        next_incomplete_screen_id="common",
        placeholder_role=True,
    )
    assert "плейсхолдер" in out
    assert "Черновик начат" in out


# --- #985: --fill-common — подтверждение экрана common в том же прогоне -------


def _draft_started_result():
    from hhru_bot.create_resume import draft_verdict_result

    return draft_verdict_result("draft_started", "common", "", new_resume_id=NEW_ID)


def _fill_common_run(env, tmp_path, capsys, monkeypatch, common_result, verdicts):
    """Боевой прогон с --fill-common: readback дважды, confirm — двойник."""
    import hhru_bot.commands.create_resume as command_module
    from hhru_bot.common import CommonResult

    verdicts = list(verdicts)
    monkeypatch.setattr(
        command_module,
        "apply_draft_readback",
        lambda page, result: verdicts.pop(0) if verdicts else result,
    )
    confirm_calls = []

    def confirm(page, resume_id, *, before_click=None):
        confirm_calls.append(resume_id)
        if isinstance(common_result, CommonResult) and common_result.acted:
            before_click()
        return common_result

    monkeypatch.setattr(
        "hhru_bot.common.confirm_common_screen",
        confirm,
    )
    env.result = _draft_started_result()
    ok = cmd.run(_args(tmp_path, force=True, fill_common=True))
    return ok, capsys.readouterr().out, confirm_calls


def test_fill_common_confirms_screen_and_rereads_ready_verdict(env, tmp_path, capsys, monkeypatch):
    from hhru_bot.common import CommonResult
    from hhru_bot.create_resume import draft_verdict_result

    ready = draft_verdict_result("ready_to_publish", None, "", new_resume_id=NEW_ID)
    ok, out, calls = _fill_common_run(
        env,
        tmp_path,
        capsys,
        monkeypatch,
        CommonResult(True, "экран common подтверждён", True),
        [_draft_started_result(), ready],
    )
    assert ok is False
    assert calls == [NEW_ID]
    assert "Подтверждаю экран common" in out
    assert "[OK] Готово к публикации" in out
    assert "[OK] Черновик начат" not in out


def test_fill_common_prefill_refusal_keeps_draft_started_verdict(
    env, tmp_path, capsys, monkeypatch
):
    from hhru_bot.common import CommonResult

    ok, out, calls = _fill_common_run(
        env,
        tmp_path,
        capsys,
        monkeypatch,
        CommonResult(False, "экран common не предзаполнен профилем аккаунта: имя"),
        [_draft_started_result()],
    )
    assert ok is True
    assert calls == [NEW_ID]
    assert "[FAIL] --fill-common: экран common не предзаполнен" in out
    # Вердикт создания напечатан и остался draft_started.
    assert "[OK] Черновик начат" in out
    assert f"Новый resume_id: {NEW_ID}" in out


def test_fill_common_uncertain_records_edit_common_marker(env, tmp_path, capsys, monkeypatch):
    from hhru_bot.common import CommonResult

    ok, _out, _calls = _fill_common_run(
        env,
        tmp_path,
        capsys,
        monkeypatch,
        CommonResult(False, "переход с экрана common не подтверждён", True, True),
        [_draft_started_result()],
    )
    assert ok is True
    history = History(tmp_path / "history.db")
    assert history.has_unresolved_uncertain(NEW_ID, "edit_common")


def test_fill_common_skipped_when_readiness_is_not_draft_started_common(
    env, tmp_path, capsys, monkeypatch
):
    from hhru_bot.create_resume import draft_verdict_result

    ready = draft_verdict_result("ready_to_publish", None, "", new_resume_id=NEW_ID)
    ok, out, calls = _fill_common_run(env, tmp_path, capsys, monkeypatch, None, [ready])
    assert ok is False
    assert calls == []
    assert "Подтверждаю экран common" not in out


def test_fill_common_not_run_in_dry_run(env, tmp_path, capsys, monkeypatch):
    confirm_calls = []
    monkeypatch.setattr(
        "hhru_bot.common.confirm_common_screen",
        lambda *args, **kwargs: confirm_calls.append(1),
    )
    env.result = CreateResumeResult(True, reason="dry-run; визард найден, клики не выполнены")
    cmd.run(_args(tmp_path, dry_run=True, fill_common=True))
    out = capsys.readouterr().out
    assert confirm_calls == []
    assert "[DRY-RUN] В боевом режиме --fill-common" in out

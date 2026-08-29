"""Контракт report-vacancy: подтверждение, аудит, всегда [FAIL] (issue #745)."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.report_vacancy as cmd
import hhru_bot.report_vacancy
from hhru_bot.history import History
from hhru_bot.report_vacancy import ReportVacancyResult

pytestmark = pytest.mark.integration

VACANCY_ID = "136672001"
REASON = "DOUBTFUL_VACANCY"


def _config(tmp_path):
    return SimpleNamespace(storage_state_file=tmp_path / "session.json", user_agent=None)


def _args(tmp_path, **overrides):
    values = dict(
        config="unused",
        history=str(tmp_path / "history.db"),
        headless=True,
        vacancy_id=VACANCY_ID,
        reason=REASON,
        comment="test comment",
        dry_run=False,
        force=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = SimpleNamespace(
        result=ReportVacancyResult(
            VACANCY_ID, REASON, success=False, reason_text="форма заполнена, не отправлена"
        )
    )
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _: _config(tmp_path))

    @contextmanager
    def launch(*args, **kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr(hhru_bot.browser, "launch_context", launch)

    def report(page, vacancy_id, reason, comment, dry_run, *, before_click=None):  # noqa: ANN001, ARG001
        if not dry_run and before_click is not None:
            before_click()
        return state.result

    monkeypatch.setattr(hhru_bot.report_vacancy, "report_vacancy_on_hh", report)
    return state


def test_no_flags_is_dry_run(env, tmp_path, capsys):
    cmd.run(_args(tmp_path, dry_run=False, force=False))
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_empty_comment_fails_before_any_browser_work(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path, comment="   ", force=True))
    assert exc.value.code == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_live_run_always_fails_and_never_writes_success(env, tmp_path, capsys):
    """Design contract (issue #745): this command never reports success —
    step 3 (final submit) is intentionally unimplemented and unconfirmed."""
    assert cmd.run(_args(tmp_path, force=True)) is True
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    with History(tmp_path / "history.db")._connect() as conn:
        row = conn.execute(
            "SELECT status FROM actions WHERE vacancy_id = ?", (VACANCY_ID,)
        ).fetchone()
    assert row is not None
    assert row["status"] != "success"


def test_force_false_stays_dry_run_regardless_of_dry_run_flag(env, tmp_path, capsys):
    # --force is the sole switch out of dry-run (same contract as
    # delete-resume): --dry-run=False alone must not reach a live run.
    cmd.run(_args(tmp_path, force=False, dry_run=False))
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_unresolved_uncertain_blocks_retry_for_same_vacancy_only(env, tmp_path, capsys):
    history = History(tmp_path / "history.db")
    history.record_action(VACANCY_ID, VACANCY_ID, "report_vacancy", "uncertain", "клик мог уйти")

    with pytest.raises(SystemExit) as exc:
        cmd.run(_args(tmp_path, force=True))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "uncertain" in out

    # A different vacancy_id must NOT be blocked by another vacancy's
    # unresolved uncertain marker (per-vacancy scoping, not account-wide).
    other_vacancy = "999999999"
    assert cmd.run(_args(tmp_path, vacancy_id=other_vacancy, force=True)) is True
    out2 = capsys.readouterr().out
    assert "Предыдущая попытка" not in out2

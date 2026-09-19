"""Pre-flight auth-гейт команды apply (#1140).

apply (боевой и --dry-run) при подтверждённо невалидной сессии отказывает
до поиска и цикла откликов — в стиле гейта list-resumes (#1129), — а не
молча работает анонимом. Решение «отказ, не предупреждение» зафиксировано
в PR #1140: план откликов опирается на персональные данные (identity #1144,
one-click SSR #1099, верификатор negotiations), которые анониму недоступны,
поэтому анонимный dry-run — не валидный сценарий просмотра, а недостоверная
репетиция (инвариант #5).
"""

from __future__ import annotations

import argparse
import contextlib
from types import SimpleNamespace

import pytest

from hhru_bot.browser import NotAuthenticated
from hhru_bot.commands import apply as apply_command
from hhru_bot.commands._common import ApplyProgress
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.history import History

pytestmark = pytest.mark.integration


def _setup(tmp_path, *, resume_count: int = 1):
    resumes = [
        ResumeConfig(
            id=f"python-{i}",
            resume_url=f"https://hh.ru/resume/AAA11{i}",
            search=SearchFilters(text=f"python-{i}"),
        )
        for i in range(resume_count)
    ]
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=resumes,
    )
    history = History(tmp_path / "history.db")
    return config, history


def _args(**overrides) -> argparse.Namespace:
    base = {
        "dry_run": True,
        "limit": 0,
        "max_pages": 1,
        "headless": True,
        "resume": None,
        "approved": None,
        "permit": None,
        "vacancy_id": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch_browser(monkeypatch, *, session_error: Exception | None = None):
    """launch_context без реального Playwright; гейт сессии — мок-исход."""

    @contextlib.contextmanager
    def fake_launch(*args, **kwargs):  # noqa: ANN002, ANN003
        yield SimpleNamespace(new_page=lambda: object())

    def fake_session(page, **kwargs):  # noqa: ANN002, ANN003
        if session_error is not None:
            raise session_error

    import hhru_bot.browser

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fake_launch)
    monkeypatch.setattr(hhru_bot.browser, "require_authenticated_session", fake_session)


def _run_command(monkeypatch, tmp_path, args, *, resume_count: int = 1):
    config, history = _setup(tmp_path, resume_count=resume_count)
    recorded: dict = {"run_for_resume_calls": 0}

    def fake_run_for_resume(page, cfg, resume, hist, throttle, a, *rest, **kw):
        recorded["run_for_resume_calls"] += 1
        recorded["page"] = page
        return False

    monkeypatch.setattr(apply_command, "run_apply_for_resume", fake_run_for_resume)
    failed = apply_command._run(args, config, history, ApplyProgress())
    return failed, recorded


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "live"])
def test_invalid_session_fails_before_search_and_cycle(monkeypatch, tmp_path, capsys, dry_run):
    """Невалидная сессия: [FAIL] в стиле #1129, ни поиск, ни цикл откликов.

    Два резюме в конфиге — гейт стоит ДО per-resume цикла: при мёртвой сессии
    run_apply_for_resume не вызывается ни разу, а не фейлится по вакансии.
    """
    _patch_browser(
        monkeypatch,
        session_error=NotAuthenticated(
            "cookie hhtoken не найден — сессия истекла (запустите `login`, затем повторите)"
        ),
    )
    failed, recorded = _run_command(monkeypatch, tmp_path, _args(dry_run=dry_run), resume_count=2)

    assert failed is True
    assert recorded["run_for_resume_calls"] == 0
    out = capsys.readouterr().out
    assert "[FAIL] Сессия недействительна" in out
    assert "login" in out
    assert "[DRY-RUN]" not in out


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "live"])
def test_valid_session_does_not_block_run(monkeypatch, tmp_path, dry_run):
    """Живая сессия: гейт проходит, боевой путь и dry-run не менялись (#1140 п.2)."""
    _patch_browser(monkeypatch, session_error=None)
    failed, recorded = _run_command(monkeypatch, tmp_path, _args(dry_run=dry_run))

    assert failed is False
    assert recorded["run_for_resume_calls"] == 1

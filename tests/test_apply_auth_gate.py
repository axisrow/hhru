"""Pre-flight auth-гейт команды apply (#1140).

apply (боевой и --dry-run) при подтверждённо невалидной сессии отказывает
до поиска и цикла откликов, а не молча работает анонимом. Решение «отказ,
не предупреждение» зафиксировано в PR #1140: план откликов опирается на
персональные данные (identity #1144, one-click SSR #1099, верификатор
negotiations), которые анониму недоступны, поэтому анонимный dry-run —
не валидный сценарий просмотра, а недостоверная репетиция (инвариант #5).
Отказ поднимается как NotAuthenticated до run_supervised_command —
тот же класс исхода, что и поздний детект pipeline (#739).
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
def test_invalid_session_fails_before_search_and_cycle(monkeypatch, tmp_path, dry_run):
    """Невалидная сессия: NotAuthenticated поднимается до супервизора, цикл не стартует.

    Ловить исключение в _run нельзя (review PR #1152): run_supervised_command
    конвертирует его в [FAIL] с ремедиацией (login/refresh-token), detail
    в durable-леджере и CommandExitCode.SESSION_EXPIRED (78) для шедулеров —
    тот же класс исхода, что и поздний детект в pipeline (#739); голый True
    давал бы generic exit 1 и, в `run`, старт bump-стадии с мёртвой сессией.
    Два резюме в конфиге — гейт стоит ДО per-resume цикла: при мёртвой сессии
    run_apply_for_resume не вызывается ни разу, а не фейлится по вакансии.
    """
    _patch_browser(
        monkeypatch,
        session_error=NotAuthenticated(
            "cookie hhtoken не найден — сессия истекла (запустите `login`, затем повторите)"
        ),
    )
    with pytest.raises(NotAuthenticated):
        _run_command(monkeypatch, tmp_path, _args(dry_run=dry_run), resume_count=2)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "live"])
def test_valid_session_does_not_block_run(monkeypatch, tmp_path, dry_run):
    """Живая сессия: гейт проходит, боевой путь и dry-run не менялись (#1140 п.2)."""
    _patch_browser(monkeypatch, session_error=None)
    failed, recorded = _run_command(monkeypatch, tmp_path, _args(dry_run=dry_run))

    assert failed is False
    assert recorded["run_for_resume_calls"] == 1


def test_run_does_not_start_bump_after_session_expired(monkeypatch):
    """Побочный эффект гейта в `run` (review PR #1152): apply-стадия возвращает
    CommandExitCode.SESSION_EXPIRED → run.py обязан остановиться до bump —
    иначе bump стартовал бы гарантированно мёртвой сессией."""
    from hhru_bot.commands import run as run_command
    from hhru_bot.exit_codes import CommandExitCode

    monkeypatch.setattr(run_command.apply_cmd, "run", lambda _args: CommandExitCode.SESSION_EXPIRED)
    monkeypatch.setattr(
        run_command.bump_cmd,
        "run",
        lambda _args: pytest.fail("bump must not run after SESSION_EXPIRED apply"),
    )

    assert run_command.run(argparse.Namespace()) is CommandExitCode.SESSION_EXPIRED

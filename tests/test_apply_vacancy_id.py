"""Точечный отклик ``apply --vacancy-id`` (#1085).

Альтернативный источник кандидата — страница самой вакансии, минуя поиск;
дальше штатный пайплайн (дедуп, фильтры, лимиты) через cards_override —
тот же маршрут, что у ``--approved``.
"""

from __future__ import annotations

import argparse
import contextlib
from types import SimpleNamespace

import pytest

from hhru_bot.commands import apply as apply_command
from hhru_bot.commands import apply_service
from hhru_bot.commands._common import ApplyProgress, run_apply_for_resume
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.history import History
from hhru_bot.search import VacancyCard, VacancyPageUnavailable
from hhru_bot.throttle import Throttle

pytestmark = pytest.mark.integration


def _setup(tmp_path):
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    return config, resume, history


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


def _fake_browser(monkeypatch):
    """launch_context без реального Playwright: пустая страница-заглушка."""

    @contextlib.contextmanager
    def fake_launch(*args, **kwargs):  # noqa: ANN002, ANN003
        yield SimpleNamespace(new_page=lambda: object())

    import hhru_bot.browser

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fake_launch)


def _run_command(monkeypatch, tmp_path, args, *, card=None, error=None, applied_calls=None):
    """Запускает apply_command._run с замоканным браузером и источником карточки.

    run_apply_for_resume подменяется рекордером: цель тестов этой команды —
    маршрут (карточка дошла / не дошла / флаги несовместимы), а не сам
    пайплайн, у которого свои тесты.
    """
    import hhru_bot.search

    config, _resume, history = _setup(tmp_path)

    if error is not None:
        monkeypatch.setattr(
            hhru_bot.search, "fetch_vacancy_card", lambda page, vid: (_ for _ in ()).throw(error)
        )
    else:
        monkeypatch.setattr(hhru_bot.search, "fetch_vacancy_card", lambda page, vid: card)

    recorded = {}

    def fake_run_for_resume(page, cfg, resume, hist, throttle, a, cards_override=None, **kw):
        recorded["cards"] = cards_override
        recorded["resume"] = resume.id
        return False

    monkeypatch.setattr(apply_command, "run_apply_for_resume", fake_run_for_resume)
    if applied_calls is not None:
        monkeypatch.setattr(
            apply_service,
            "apply_to_vacancy",
            lambda *a, **k: applied_calls.append(a[1].vacancy_id),
        )
    _fake_browser(monkeypatch)
    failed = apply_command._run(args, config, history, ApplyProgress())
    return failed, recorded


def _card(vacancy_id="132283257"):
    return VacancyCard(vacancy_id, "QA Engineer", "Acme", f"https://hh.ru/vacancy/{vacancy_id}")


def test_vacancy_id_incompatible_with_limit(monkeypatch, tmp_path, capsys):
    failed, recorded = _run_command(monkeypatch, tmp_path, _args(vacancy_id="123", limit=2))
    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out
    assert recorded == {}


def test_vacancy_id_incompatible_with_approved(monkeypatch, tmp_path, capsys):
    failed, recorded = _run_command(
        monkeypatch, tmp_path, _args(vacancy_id="123", approved=7, permit="p")
    )
    assert failed is True
    assert "--approved" in capsys.readouterr().out
    assert recorded == {}


def test_vacancy_id_rejects_non_numeric(monkeypatch, tmp_path, capsys):
    failed, recorded = _run_command(monkeypatch, tmp_path, _args(vacancy_id="abc"))
    assert failed is True
    assert "[FAIL]" in capsys.readouterr().out
    assert recorded == {}


def test_vacancy_id_happy_path(monkeypatch, tmp_path, capsys):
    card = _card()
    failed, recorded = _run_command(
        monkeypatch, tmp_path, _args(vacancy_id=card.vacancy_id), card=card
    )
    assert failed is False
    assert recorded["cards"] == [card]
    assert recorded["resume"] == "python"
    out = capsys.readouterr().out
    assert f"[OK] Целевая вакансия: {card.title}" in out


def test_vacancy_id_closed_vacancy_is_fail_not_crash(monkeypatch, tmp_path, capsys):
    error = VacancyPageUnavailable("132283257", "confirmed")
    failed, recorded = _run_command(
        monkeypatch, tmp_path, _args(vacancy_id="132283257"), error=error
    )
    assert failed is True
    out = capsys.readouterr().out
    assert "[FAIL] Вакансия недоступна" in out
    assert recorded == {}


def test_vacancy_id_dedup_skips_already_applied(tmp_path, monkeypatch, capsys):
    """Та же has_applied-дедупликация, что у обычного apply: повторный прогон
    по той же вакансии не доходит до apply_to_vacancy."""
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    card = _card()
    history.record_action(resume.resume_id, card.vacancy_id, "apply", "success", "ранее откликнуты")

    applied_calls: list[str] = []
    monkeypatch.setattr(
        apply_service, "apply_to_vacancy", lambda *a, **k: applied_calls.append(a[1].vacancy_id)
    )
    monkeypatch.setattr(apply_service, "resolve_numeric_resume_ids", lambda _page: None)

    failed = run_apply_for_resume(
        object(),
        config,
        resume,
        history,
        throttle,
        _args(vacancy_id=card.vacancy_id),
        cards_override=[card],
    )
    assert failed is False
    assert applied_calls == []

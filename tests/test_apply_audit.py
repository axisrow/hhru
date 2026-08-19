"""TDD regression coverage for apply attempts that outlive the Python stack.

The browser can leave an apply dump on disk and the process can disappear before
the post-submit result is written.  The audit marker must therefore be durable
before entering the browser action, not created only after it returns.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from hhru_bot.apply.antibot import AntiBotChallengeDetected, AntiBotDetection
from hhru_bot.commands import _common
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.history import History
from hhru_bot.search import VacancyCard
from hhru_bot.throttle import Throttle

pytestmark = pytest.mark.integration


def test_apply_crash_after_planning_leaves_durable_uncertain_audit(tmp_path, monkeypatch):
    """A process-like crash must not leave a submit candidate absent from history."""
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
    args = argparse.Namespace(
        config=None,
        resume=None,
        dry_run=False,
        headless=True,
        max_pages=1,
        limit=1,
    )
    card = VacancyCard(
        vacancy_id="123",
        title="Python developer",
        company="Acme",
        url="https://hh.ru/vacancy/123",
    )

    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    monkeypatch.setattr(_common, "search_vacancies", lambda *a, **k: [card])

    class ProcessLikeCrash(BaseException):
        pass

    def crash_before_result(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs["before_submit"]()
        raise ProcessLikeCrash("worker disappeared after browser-side work")

    monkeypatch.setattr(_common, "apply_to_vacancy", crash_before_result)

    with pytest.raises(ProcessLikeCrash):
        _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    with history._connect() as conn:
        row = conn.execute("SELECT resume_id, vacancy_id, action, status FROM actions").fetchone()

    assert row is not None
    assert tuple(row) == ("AAA111", "123", "apply", "uncertain")
    assert history.has_applied("AAA111", "123")


def test_apply_finalizes_the_pre_submit_marker_in_place(tmp_path, monkeypatch):
    """A normal result updates the reservation instead of appending a duplicate."""
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
    args = argparse.Namespace(
        config=None,
        resume=None,
        dry_run=False,
        headless=True,
        max_pages=1,
        limit=1,
    )
    card = VacancyCard(
        vacancy_id="123",
        title="Python developer",
        company="Acme",
        url="https://hh.ru/vacancy/123",
    )

    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    monkeypatch.setattr(_common, "search_vacancies", lambda *a, **k: [card])

    def succeeds_after_reservation(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs["before_submit"]()
        return SimpleNamespace(
            success=True,
            reason="success",
            letter_variant="template",
            skipped=False,
            acted=True,
            uncertain=False,
            skip_reason=None,
        )

    monkeypatch.setattr(_common, "apply_to_vacancy", succeeds_after_reservation)

    _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    with history._connect() as conn:
        rows = conn.execute("SELECT resume_id, vacancy_id, action, status FROM actions").fetchall()

    assert [tuple(row) for row in rows] == [("AAA111", "123", "apply", "success")]


def test_post_submit_challenge_finalizes_uncertain_marker_before_stopping(tmp_path, monkeypatch):
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
    args = argparse.Namespace(dry_run=False, headless=True, max_pages=1, limit=1)
    card = VacancyCard("123", "Python developer", "Acme", "https://hh.ru/vacancy/123")

    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    monkeypatch.setattr(_common, "search_vacancies", lambda *a, **k: [card])
    detection = AntiBotDetection("url_path", "URL содержит /captcha")

    def challenge_after_reservation(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs["before_submit"]()
        raise AntiBotChallengeDetected(detection)

    monkeypatch.setattr(_common, "apply_to_vacancy", challenge_after_reservation)

    with pytest.raises(AntiBotChallengeDetected):
        _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    with history._connect() as conn:
        row = conn.execute("SELECT status, reason FROM actions").fetchone()

    assert row is not None
    assert row["status"] == "uncertain"
    assert "обнаружена анти-бот проверка" in row["reason"]

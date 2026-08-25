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
from hhru_bot.config_sections.scoring import ScoringConfig
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


def test_approved_apply_attributes_to_the_query_recorded_at_enqueue_time(tmp_path, monkeypatch):
    """#420 follow-up (Codex adversarial-review round 1, PR #449): review_queue
    (#414 schema) didn't persist the search_query a card was found under, so
    the config's *current* resume.search.text got written instead — wrong
    whenever the config changed between the dry-run that queued the card and
    a later `apply --approved` run. The fix persists the query at enqueue
    time and uses that stored value, not whatever the config says now.
    """
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="devops"),  # config changed after enqueue_review
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    card = VacancyCard(
        vacancy_id="123",
        title="Python developer",
        company="Acme",
        url="https://hh.ru/vacancy/123",
    )
    # dry-run under "python" queued the card; that query must survive to the
    # later approved apply, made under a config that now says "devops".
    item_id = history.enqueue_review("AAA111", card, 1.0, {}, "cover letter", search_query="python")
    permit = history.approve_review(item_id)
    args = argparse.Namespace(
        config=None,
        resume=None,
        dry_run=False,
        headless=True,
        max_pages=1,
        limit=1,
        approved=item_id,
        permit=permit,
    )

    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)

    def succeeds_after_reservation(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs["before_submit"]()
        return SimpleNamespace(
            success=True,
            reason="success",
            letter_variant="approved",
            skipped=False,
            acted=True,
            uncertain=False,
            skip_reason=None,
        )

    monkeypatch.setattr(_common, "apply_to_vacancy", succeeds_after_reservation)

    _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    with history._connect() as conn:
        row = conn.execute("SELECT search_query FROM actions WHERE vacancy_id='123'").fetchone()

    assert row is not None
    assert row["search_query"] == "python"


def test_approved_apply_from_pre_migration_queue_row_falls_back_to_vacancies_seen(
    tmp_path, monkeypatch
):
    """Codex adversarial-review round 2 (PR #449): review_queue rows created
    before this fix shipped have no stored search_query (the column didn't
    exist yet) and are attributed via the existing vacancies_seen fallback in
    funnel_by_search_query — exactly the pre-PR `main` behavior for every
    action (#420: "keep vacancies_seen as fallback rather than backfilling
    historical actions"). This is a one-time migration-window case, not a new
    defect: rows enqueued *after* this fix always carry their real query (see
    test_approved_apply_attributes_to_the_query_recorded_at_enqueue_time), so
    the fallback here only ever applies to the finite backlog of queue
    entries that predate the column.
    """
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="devops"),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    card = VacancyCard(
        vacancy_id="123",
        title="Python developer",
        company="Acme",
        url="https://hh.ru/vacancy/123",
    )
    # This vacancy was independently seen by `search` under two unrelated queries.
    now = "2026-01-01T00:00:00"
    with history._connect() as conn:
        conn.execute(
            "INSERT INTO vacancies_seen (vacancy_id, search_query, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            ("123", "python", now, now),
        )
        conn.execute(
            "INSERT INTO vacancies_seen (vacancy_id, search_query, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            ("123", "backend", now, now),
        )
    # Pre-fix queue row: no search_query recorded (legacy row, provenance unknown).
    item_id = history.enqueue_review("AAA111", card, 1.0, {}, "cover letter")
    permit = history.approve_review(item_id)
    args = argparse.Namespace(
        config=None,
        resume=None,
        dry_run=False,
        headless=True,
        max_pages=1,
        limit=1,
        approved=item_id,
        permit=permit,
    )

    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)

    def succeeds_after_reservation(*args, **kwargs):  # noqa: ANN002, ANN003
        kwargs["before_submit"]()
        return SimpleNamespace(
            success=True,
            reason="success",
            letter_variant="approved",
            skipped=False,
            acted=True,
            uncertain=False,
            skip_reason=None,
        )

    monkeypatch.setattr(_common, "apply_to_vacancy", succeeds_after_reservation)

    _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    funnel = history.funnel_by_search_query()
    attributed_to_seen_queries = {
        row["search_query"] for row in funnel if row["search_query"] in {"python", "backend"}
    }
    assert attributed_to_seen_queries == {"python", "backend"}, (
        "pre-migration queue row should still fall back to vacancies_seen, "
        f"got {attributed_to_seen_queries}"
    )


def test_approved_apply_blocks_current_employer(tmp_path, monkeypatch):
    """#524 safety-гейт действует и на явном пути --approved: запись очереди,
    одобренная до настройки account.current_employer (или до смены работодателя),
    не должна уйти текущему работодателю — отклик необратим."""
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="devops", current_employers=["ООО Пример"]),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    card = VacancyCard(
        vacancy_id="123",
        title="Python developer",
        company="ООО ПРИМЕР-Строй",  # текущий работодатель: casefold-подстрока
        url="https://hh.ru/vacancy/123",
    )
    item_id = history.enqueue_review("AAA111", card, 1.0, {}, "cover letter", search_query="devops")
    permit = history.approve_review(item_id)
    args = argparse.Namespace(
        config=None,
        resume=None,
        dry_run=False,
        headless=True,
        max_pages=1,
        limit=1,
        approved=item_id,
        permit=permit,
    )

    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    applied: list[bool] = []

    def fail_if_applied(*args, **kwargs):  # noqa: ANN002, ANN003
        applied.append(True)
        return SimpleNamespace(
            success=True,
            reason="success",
            letter_variant="approved",
            skipped=False,
            acted=True,
            uncertain=False,
            skip_reason=None,
        )

    monkeypatch.setattr(_common, "apply_to_vacancy", fail_if_applied)

    _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    assert applied == []  # отклик текущему работодателю не отправлен
    with history._connect() as conn:
        review_status = conn.execute(
            "SELECT status FROM review_queue WHERE id=?", (item_id,)
        ).fetchone()
        skip_row = conn.execute(
            "SELECT reason FROM skipped WHERE resume_id='AAA111' AND vacancy_id='123'"
        ).fetchone()
    assert review_status["status"] == "skipped"
    assert skip_row["reason"] == "current_employer"


def test_approved_apply_bypasses_letter_threshold(tmp_path, monkeypatch):
    """#648: an explicitly approved card bypasses automated letter filtering.

    Review cards are reconstructed without ``vacancy_text``; applying the
    configured threshold to that path would reject a human-approved letter
    based on incomplete data.
    """
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python"),
        scoring=ScoringConfig(letter_match_threshold=95.0),
    )
    config = AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0),
        cover_letter_default="hello",
        resumes=[resume],
    )
    history = History(tmp_path / "history.db")
    throttle = Throttle(config.throttle, history)
    card = VacancyCard(
        vacancy_id="123",
        title="Python developer",
        company="Acme",
        url="https://hh.ru/vacancy/123",
    )
    item_id = history.enqueue_review("AAA111", card, 80.0, {}, "approved letter")
    permit = history.approve_review(item_id)
    args = argparse.Namespace(
        config=None,
        resume=None,
        dry_run=False,
        headless=True,
        max_pages=1,
        limit=1,
        approved=item_id,
        permit=permit,
    )

    monkeypatch.setattr(_common, "resolve_numeric_resume_ids", lambda _page: None)
    captured: dict = {}

    def apply(*args, **kwargs):  # noqa: ANN001, ARG001
        captured.update(kwargs)
        kwargs["before_submit"]()
        return SimpleNamespace(
            success=True,
            reason="success",
            letter_variant="approved",
            skipped=False,
            acted=True,
            uncertain=False,
            skip_reason=None,
        )

    monkeypatch.setattr(_common, "apply_to_vacancy", apply)

    _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    assert "letter_match_threshold" not in captured


def test_apply_preserves_numeric_letter_threshold(tmp_path, monkeypatch):
    """#648: threshold assignment must preserve its configured numeric value."""
    resume = ResumeConfig(
        id="python",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text="python"),
        scoring=ScoringConfig(letter_match_threshold=42.5),
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
    captured: dict = {}

    def apply(*args, **kwargs):  # noqa: ANN001, ARG001
        captured.update(kwargs)
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

    monkeypatch.setattr(_common, "apply_to_vacancy", apply)

    _common.run_apply_for_resume(object(), config, resume, history, throttle, args)

    assert captured["letter_match_threshold"] == 42.5

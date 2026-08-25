from __future__ import annotations

import logging
import signal
import sqlite3
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot.commands.competitors import (
    _observed_eta,
    _progress,
    _throttle_estimate,
    run_collect,
)
from hhru_bot.competitors import (
    CompetitorResume,
    CompetitorSearchCard,
    CompetitorSearchCoverage,
)
from hhru_bot.exit_codes import CommandExitCode
from hhru_bot.history import History

pytestmark = pytest.mark.integration


class _Context:
    def new_page(self):
        return object()


@contextmanager
def _launch(*_args, **_kwargs):
    yield _Context()


class _Throttle:
    def __init__(self, *_args, **_kwargs):
        pass

    def wait(self, _reason):
        pass


def _args(tmp_path: Path, *, resume: bool = False) -> Namespace:
    return Namespace(
        text="AI",
        max_pages=1,
        resume=resume,
        execution_mode="foreground",
        progress_verbosity=1,
        items_per_page=100,
        config=str(tmp_path / "config.yaml"),
        history=str(tmp_path / "history.db"),
        headless=True,
        quiet=False,
    )


def _patch_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _path: SimpleNamespace(
            storage_state_file=Path("session.json"),
            user_agent=None,
            throttle=SimpleNamespace(min_delay_seconds=8, max_delay_seconds=25),
        ),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", _launch)
    monkeypatch.setattr("hhru_bot.throttle.Throttle", _Throttle)


@pytest.mark.parametrize(
    ("signum", "expected"),
    [
        (signal.SIGTERM, CommandExitCode.SIGTERM),
        pytest.param(
            getattr(signal, "SIGHUP", signal.SIGTERM),
            CommandExitCode.SIGHUP if hasattr(signal, "SIGHUP") else CommandExitCode.SIGTERM,
            id="sighup",
        ),
    ],
)
def test_signal_finalizes_partial_checkpoint(tmp_path, monkeypatch, signum, expected):
    _patch_runtime(monkeypatch)

    def terminate_on_navigation(*_args, **_kwargs):
        signal.raise_signal(signum)

    monkeypatch.setattr("hhru_bot.browser.goto_hh", terminate_on_navigation)

    result = run_collect(_args(tmp_path))

    assert result is expected
    row = History(tmp_path / "history.db").competitor_collection_runs()[0]
    assert row["status"] == "partial"
    assert row["exit_code"] == expected.value
    assert row["last_started_page"] == 0
    assert row["resume_page"] == 0
    assert "SignalTermination" in row["detail"]


def test_browser_crash_finalizes_run_before_propagating(tmp_path, monkeypatch):
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "hhru_bot.browser.goto_hh",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PlaywrightError("browser closed")),
    )

    with pytest.raises(PlaywrightError, match="browser closed"):
        run_collect(_args(tmp_path))

    row = History(tmp_path / "history.db").competitor_collection_runs()[0]
    assert row["status"] == "partial"
    assert row["exit_code"] == 1
    assert row["resume_page"] == 0
    assert "browser closed" in row["detail"]


def test_resume_starts_after_last_completed_page(tmp_path, monkeypatch):
    history = History(tmp_path / "history.db")
    previous = history.start_competitor_collection("AI", 2)
    history.finish_competitor_collection(
        previous,
        status="limited",
        pages_fetched=2,
        cards_seen=40,
        details_saved=40,
        details_failed=0,
        resume_page=2,
        last_started_page=1,
        last_completed_page=1,
        observed_page_size=20,
    )
    _patch_runtime(monkeypatch)
    visited: list[int] = []
    rank_offsets: list[int] = []

    def goto(_page, url):
        visited.append(int(parse_qs(urlsplit(url).query)["page"][0]))

    monkeypatch.setattr("hhru_bot.browser.goto_hh", goto)

    def parse_page(_page, *, rank_offset, expected_page_size):
        assert expected_page_size == 100
        rank_offsets.append(rank_offset)
        return []

    monkeypatch.setattr("hhru_bot.competitors.parse_search_page", parse_page)
    monkeypatch.setattr("hhru_bot.competitors.has_next_search_page", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "hhru_bot.competitors.inspect_search_coverage",
        lambda *_a, **_k: CompetitorSearchCoverage(0, 1, False, 0),
    )

    assert run_collect(_args(tmp_path, resume=True)) is False
    assert visited == [2]
    assert rank_offsets == [40]
    latest = history.competitor_collection_runs()[-1]
    assert latest["resumed_from_run_id"] == previous
    assert latest["last_completed_page"] == 2


def test_progress_survives_closed_stdout_and_still_writes_file(tmp_path, monkeypatch):
    log_path = tmp_path / "hhru.log"
    root = logging.getLogger("hhru_bot")
    previous = list(root.handlers)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    root.handlers = [handler]
    monkeypatch.setattr(
        "builtins.print", lambda *_a, **_k: (_ for _ in ()).throw(BrokenPipeError())
    )
    try:
        _progress("[HEARTBEAT] durable", quiet=False)
    finally:
        handler.close()
        root.handlers = previous

    assert "[HEARTBEAT] durable" in log_path.read_text()


def test_progress_verbosity_zero_keeps_final_summary(tmp_path, monkeypatch, capsys):
    _patch_runtime(monkeypatch)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *_a, **_k: None)
    monkeypatch.setattr("hhru_bot.competitors.parse_search_page", lambda *_a, **_k: [])
    monkeypatch.setattr("hhru_bot.competitors.has_next_search_page", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "hhru_bot.competitors.inspect_search_coverage",
        lambda *_a, **_k: CompetitorSearchCoverage(0, 1, False, 0),
    )
    args = _args(tmp_path)
    args.progress_verbosity = 0

    assert run_collect(args) is False

    output = capsys.readouterr().out
    assert "[START]" not in output
    assert "[PROGRESS]" not in output
    assert "Конкуренты:" in output
    assert "код завершения=0" in output
    assert "время=" in output


def test_global_quiet_overrides_progress_verbosity(tmp_path, monkeypatch, capsys):
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(
        "hhru_bot.browser.goto_hh",
        lambda *_a, **_k: (_ for _ in ()).throw(PlaywrightError("browser closed")),
    )
    args = _args(tmp_path)
    args.quiet = True

    with pytest.raises(PlaywrightError, match="browser closed"):
        run_collect(args)

    output = capsys.readouterr().out
    assert "[START]" not in output
    assert "[STOP]" in output
    assert "browser closed" in output
    assert "время=" in output


def test_variable_page_sizes_update_volume_and_privacy_rejection_does_not_abort(
    tmp_path, monkeypatch, capsys
):
    _patch_runtime(monkeypatch)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *_a, **_k: None)
    page_sizes = iter((20, 100))

    def parse_page(_page, *, rank_offset, expected_page_size):
        assert expected_page_size == 100
        return [
            CompetitorSearchCard(
                resume_id=f"r{rank_offset + index}",
                resume_url=f"https://hh.ru/resume/r{rank_offset + index}",
                desired_role="AI Engineer",
                rank=rank_offset + index + 1,
            )
            for index in range(next(page_sizes))
        ]

    monkeypatch.setattr("hhru_bot.competitors.parse_search_page", parse_page)
    next_pages = iter((True, False))
    monkeypatch.setattr(
        "hhru_bot.competitors.has_next_search_page", lambda *_a, **_k: next(next_pages)
    )
    monkeypatch.setattr(
        "hhru_bot.competitors.inspect_search_coverage",
        lambda *_a, **_k: CompetitorSearchCoverage(120, 2, False, 20),
    )
    monkeypatch.setattr(
        "hhru_bot.competitors.fetch_competitor_resume",
        lambda _page, card: CompetitorResume(
            resume_id=card.resume_id,
            resume_url=card.resume_url,
            desired_role=card.desired_role,
        ),
    )
    calls = 0

    def upsert(_self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 21:
            raise sqlite3.IntegrityError("contact-like competitor skill")
        return "new"

    monkeypatch.setattr(History, "upsert_competitor_resume", upsert)
    args = _args(tmp_path)
    args.max_pages = 2

    assert run_collect(args) is True

    output = capsys.readouterr().out
    assert "объём~120 деталей" in output
    assert "страница=2, карточек=120, деталей=120" in output
    assert "IntegrityError: contact-like competitor skill" in output
    row = History(tmp_path / "history.db").competitor_collection_runs()[0]
    assert row["status"] == "partial"
    assert row["cards_seen"] == 120
    assert row["details_saved"] == 119
    assert row["details_failed"] == 1
    assert row["observed_page_size"] == 100


def test_estimate_reports_requested_and_observed_page_size():
    estimate = _throttle_estimate(
        details=100,
        requested_page_size=100,
        observed_page_size=20,
        min_delay=8,
        max_delay=25,
    )

    assert "запрошено=100/стр., фактически=20/стр." in estimate
    assert "объём~100 деталей" in estimate
    assert "13 мин-41 мин" in estimate
    assert "ETA уточнится" in estimate

    one_detail = _throttle_estimate(
        details=1,
        requested_page_size=1,
        observed_page_size=1,
        min_delay=8,
        max_delay=25,
    )
    assert "троттлинга 0 с-0 с" in one_detail


def test_observed_eta_uses_completed_detail_rate():
    eta = _observed_eta(
        {"saved": 10, "failed": 0, "expected_details": 100},
        elapsed=200,
    )

    assert eta == "осталось~30 мин (диапазон 22 мин-38 мин)"


def test_observed_eta_waits_for_three_details_and_stops_at_completion():
    state = {"saved": 2, "failed": 0, "expected_details": 100}
    assert _observed_eta(state, elapsed=40) is None

    state = {"saved": 99, "failed": 1, "expected_details": 100}
    assert _observed_eta(state, elapsed=2000) is None

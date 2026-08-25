from __future__ import annotations

import logging
import signal
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot.commands.competitors import _progress, run_collect
from hhru_bot.competitors import CompetitorSearchCoverage
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
        config=str(tmp_path / "config.yaml"),
        history=str(tmp_path / "history.db"),
        headless=True,
        quiet=False,
    )


def _patch_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _path: SimpleNamespace(
            storage_state_file=Path("session.json"), user_agent=None, throttle=None
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

    def goto(_page, url):
        visited.append(int(parse_qs(urlsplit(url).query)["page"][0]))

    monkeypatch.setattr("hhru_bot.browser.goto_hh", goto)
    monkeypatch.setattr("hhru_bot.competitors.parse_search_page", lambda *_a, **_k: [])
    monkeypatch.setattr("hhru_bot.competitors.has_next_search_page", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "hhru_bot.competitors.inspect_search_coverage",
        lambda *_a, **_k: CompetitorSearchCoverage(0, 1, False, 0),
    )

    assert run_collect(_args(tmp_path, resume=True)) is False
    assert visited == [2]
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

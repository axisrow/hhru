from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import asdict
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


class _WorkerPool:
    def __init__(self, workers, config):
        self.workers = workers
        self.config = config
        self.results = []

    @property
    def size(self):
        return self.workers

    def start(self):
        pass

    def grow(self, target_workers):
        self.workers = max(self.workers, target_workers)

    def submit(self, task_id, card):
        from hhru_bot.competitors import fetch_competitor_resume

        try:
            snapshot = fetch_competitor_resume(
                object(),
                card,
                require_authentication=self.config.require_authentication,
            )
        except Exception as exc:
            self.results.append(
                {
                    "kind": "error",
                    "worker_id": 0,
                    "task_id": task_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        else:
            payload = asdict(snapshot)
            payload["content_hash"] = snapshot.content_hash()
            self.results.append(
                {
                    "kind": "success",
                    "worker_id": 0,
                    "task_id": task_id,
                    "payload": payload,
                }
            )

    def result(self, **_kwargs):
        return self.results.pop(0)

    def close(self, **_kwargs):
        pass


def _args(tmp_path: Path, *, resume: bool = False) -> Namespace:
    return Namespace(
        text="AI",
        max_pages=1,
        resume=resume,
        execution_mode="foreground",
        progress_verbosity=1,
        items_per_page=100,
        auth_mode="anonymous",
        search_in="position",
        detail_workers=10,
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
    monkeypatch.setattr("hhru_bot.competitor_workers.DetailWorkerPool", _WorkerPool)


def test_competing_collect_returns_fail_without_traceback(tmp_path, monkeypatch, capsys):
    """A live collection lease is reported as a normal command failure."""
    _patch_runtime(monkeypatch)
    history = History(tmp_path / "history.db")
    history.start_competitor_collection("AI", 1)

    result = run_collect(_args(tmp_path))

    captured = capsys.readouterr()
    assert result is True
    assert captured.out.startswith("[FAIL] ")
    assert "Traceback" not in captured.out
    assert captured.err == ""


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


def test_authenticated_parallel_workers_are_rejected_before_run(tmp_path, monkeypatch):
    _patch_runtime(monkeypatch)
    args = _args(tmp_path)
    args.auth_mode = "authenticated"
    args.detail_workers = 10

    with pytest.raises(ValueError, match="authenticated требует --detail-workers 1"):
        run_collect(args)

    assert not (tmp_path / "history.db").exists()


@pytest.mark.parametrize(
    ("auth_mode", "expected_storage_state", "expected_authentication"),
    [
        ("anonymous", None, False),
        ("authenticated", Path("session.json"), True),
    ],
)
def test_auth_mode_controls_context_and_page_guards(
    tmp_path,
    monkeypatch,
    auth_mode,
    expected_storage_state,
    expected_authentication,
):
    _patch_runtime(monkeypatch)
    observed: dict = {}

    @contextmanager
    def launch(storage_state_file, **_kwargs):
        observed["storage_state_file"] = storage_state_file
        yield _Context()

    monkeypatch.setattr("hhru_bot.browser.launch_context", launch)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *_a, **_k: None)

    def parse_page(_page, **kwargs):
        observed["search_authentication"] = kwargs["require_authentication"]
        return [
            CompetitorSearchCard(
                resume_id="r1",
                resume_url="https://hh.ru/resume/r1",
                desired_role="AI Engineer",
                rank=1,
            )
        ]

    monkeypatch.setattr("hhru_bot.competitors.parse_search_page", parse_page)
    monkeypatch.setattr("hhru_bot.competitors.has_next_search_page", lambda *_a: False)
    monkeypatch.setattr(
        "hhru_bot.competitors.inspect_search_coverage",
        lambda *_a, **_k: CompetitorSearchCoverage(1, 1, False, 1),
    )

    def fetch(_page, card, **kwargs):
        observed["detail_authentication"] = kwargs["require_authentication"]
        return CompetitorResume(
            resume_id=card.resume_id,
            resume_url=card.resume_url,
            desired_role=card.desired_role,
        )

    monkeypatch.setattr("hhru_bot.competitors.fetch_competitor_resume", fetch)
    args = _args(tmp_path)
    args.auth_mode = auth_mode
    args.detail_workers = 1 if auth_mode == "authenticated" else 10

    assert run_collect(args) is False
    assert observed == {
        "storage_state_file": expected_storage_state,
        "search_authentication": expected_authentication,
        "detail_authentication": expected_authentication,
    }


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

    def parse_page(_page, *, rank_offset, expected_page_size, require_authentication):
        assert expected_page_size == 100
        assert require_authentication is False
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


def test_variable_page_sizes_update_volume(tmp_path, monkeypatch, capsys):
    _patch_runtime(monkeypatch)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *_a, **_k: None)
    page_sizes = iter((20, 100))

    def parse_page(_page, *, rank_offset, expected_page_size, require_authentication):
        assert expected_page_size == 100
        assert require_authentication is False
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
        lambda _page, card, **_kwargs: CompetitorResume(
            resume_id=card.resume_id,
            resume_url=card.resume_url,
            desired_role=card.desired_role,
        ),
    )
    monkeypatch.setattr(History, "upsert_competitor_resume", lambda *_a, **_k: "new")
    args = _args(tmp_path)
    args.max_pages = 2

    assert run_collect(args) is False

    output = capsys.readouterr().out
    assert "объём~120 деталей" in output
    assert "страница=2, карточек=120, деталей=120" in output
    row = History(tmp_path / "history.db").competitor_collection_runs()[0]
    assert row["status"] == "complete"
    assert row["cards_seen"] == 120
    assert row["details_saved"] == 120
    assert row["details_failed"] == 0
    assert row["observed_page_size"] == 100


_STDOUT_STREAMING_CHILD_SCRIPT = """
import os
import sys

sys.path.insert(0, {src_root!r})

from argparse import Namespace
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace

from hhru_bot.commands.competitors import run_collect
from hhru_bot.competitors import CompetitorResume, CompetitorSearchCard, CompetitorSearchCoverage
from hhru_bot.history import History
import hhru_bot.browser
import hhru_bot.competitor_workers
import hhru_bot.competitors
import hhru_bot.config
import hhru_bot.throttle

handshake_read_fd = {handshake_read_fd}


class _Context:
    def new_page(self):
        return object()


@contextmanager
def _launch(*_a, **_k):
    yield _Context()


class _Throttle:
    def __init__(self, *_a, **_k):
        pass

    def wait(self, _reason):
        pass


class _WorkerPool:
    def __init__(self, _workers, config):
        self.config = config
        self.results = []
        self._size = _workers

    @property
    def size(self):
        return self._size

    def start(self):
        pass

    def grow(self, target_workers):
        self._size = max(self._size, target_workers)

    def submit(self, task_id, card):
        snapshot = hhru_bot.competitors.fetch_competitor_resume(
            object(), card, require_authentication=self.config.require_authentication
        )
        payload = asdict(snapshot)
        payload["content_hash"] = snapshot.content_hash()
        self.results.append({{
            "kind": "success",
            "worker_id": 0,
            "task_id": task_id,
            "payload": payload,
        }})

    def result(self, **_kwargs):
        return self.results.pop(0)

    def close(self, **_kwargs):
        pass


hhru_bot.config.load_config_or_exit = lambda _path: SimpleNamespace(
    storage_state_file={config_path!r},
    user_agent=None,
    throttle=SimpleNamespace(min_delay_seconds=8, max_delay_seconds=25),
)
hhru_bot.browser.launch_context = _launch
hhru_bot.browser.goto_hh = lambda *_a, **_k: None
hhru_bot.throttle.Throttle = _Throttle
hhru_bot.competitor_workers.DetailWorkerPool = _WorkerPool


def parse_page(_page, *, rank_offset, expected_page_size, require_authentication):
    assert require_authentication is False
    return [
        CompetitorSearchCard(
            resume_id=f"r{{rank_offset + index}}",
            resume_url=f"https://hh.ru/resume/r{{rank_offset + index}}",
            desired_role="AI Engineer",
            rank=rank_offset + index + 1,
        )
        for index in range(2)
    ]


hhru_bot.competitors.parse_search_page = parse_page
_next_pages = iter((True, False))
hhru_bot.competitors.has_next_search_page = lambda *_a, **_k: next(_next_pages)
hhru_bot.competitors.inspect_search_coverage = (
    lambda *_a, **_k: CompetitorSearchCoverage(4, 2, False, 2)
)
History.upsert_competitor_resume = lambda _self, *_a, **_k: "new"

_fetch_calls = 0


def fetch(_page, card, *, require_authentication):
    global _fetch_calls
    assert require_authentication is False
    _fetch_calls += 1
    # Page 1 has exactly 2 cards -> fetch calls 1-2 are page 1's details,
    # and page 1's [PROGRESS] line is printed right after call 2 returns,
    # before page 2 starts. Block on call 3 (page 2's first detail) until
    # the parent has read that line off the stdout pipe.
    if _fetch_calls == 3:
        os.read(handshake_read_fd, 1)
    return CompetitorResume(
        resume_id=card.resume_id,
        resume_url=card.resume_url,
        desired_role=card.desired_role,
    )


hhru_bot.competitors.fetch_competitor_resume = fetch

args = Namespace(
    text="AI",
    max_pages=2,
    resume=False,
    execution_mode="foreground",
    progress_verbosity=1,
    items_per_page=100,
    auth_mode="anonymous",
    search_in="position",
    detail_workers=10,
    config={config_path!r},
    history={history_path!r},
    headless=True,
    quiet=False,
)

result = run_collect(args)
# run_collect returns True when details_failed > 0 (see its
# `return details_failed > 0`); this run induces no failures, so a clean
# run means `result is False`.
sys.exit(0 if result is False else 1)
"""


def test_stdout_streams_progress_line_by_line_before_process_completes(tmp_path):
    """Long-running collect must not buffer stdout until exit (issue #632).

    Runs `run_collect` in a real `subprocess.Popen` child (a genuine OS
    process, the same shape the plugin actually launches -- not a thread:
    `run_collect` calls `signal.signal()`, which only works in a process's
    main thread) and reads its stdout through the process's own real pipe
    (not `capsys`, which buffers in-process, and not `os.fork()`, which
    Python's docs warn can deadlock inside a multi-threaded parent such as
    a pytest-xdist worker). The child's stdout is fully block-buffered
    (`bufsize` default when not `text=True`... see below), matching what a
    real process gets once stdout is redirected to a pipe instead of a
    tty. A handshake pipe (inherited by the child via its fd number) blocks
    the child right before it starts page 2's fetch until the parent has
    actually read page 1's `[PROGRESS]` line off the stdout pipe AND
    confirmed the child PID is still alive at that moment -- proving the
    write reached the reader while the child was still running, not only
    after it had exited.
    """
    src_root = str(Path(__file__).resolve().parent.parent / "src")
    config_path = str(tmp_path / "config.yaml")
    history_path = str(tmp_path / "history.db")

    handshake_read_fd, handshake_write_fd = os.pipe()
    os.set_inheritable(handshake_read_fd, True)

    script = _STDOUT_STREAMING_CHILD_SCRIPT.format(
        src_root=src_root,
        config_path=config_path,
        history_path=history_path,
        handshake_read_fd=handshake_read_fd,
    )
    script_path = tmp_path / "stdout_streaming_child.py"
    script_path.write_text(script)

    proc = subprocess.Popen(
        [sys.executable, str(script_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=65536,  # block-buffered, not line-buffered -- see docstring
        pass_fds=(handshake_read_fd,),
    )
    os.close(handshake_read_fd)
    assert proc.stdout is not None
    assert proc.stderr is not None

    lines: list[str] = []
    deadline = time.monotonic() + 10
    page1_progress_seen_while_child_alive = False
    try:
        while True:
            line = proc.stdout.readline()
            if line == "":
                break
            lines.append(line)
            if "страница=1, карточек=2, деталей=2" in line:
                # The child is still running -- it hasn't reached page 2's
                # detail fetch yet, which is where it blocks on the
                # handshake pipe we control. Confirm the process is
                # genuinely alive right now, at the moment this line
                # reached us.
                page1_progress_seen_while_child_alive = proc.poll() is None
                os.write(handshake_write_fd, b"x")  # let page 2 proceed
                break
            if time.monotonic() > deadline:
                proc.kill()
                pytest.fail("timed out waiting for page 1 [PROGRESS] line")

        # Drain remaining output until the child exits.
        remaining = proc.stdout.read()
        lines.extend(remaining.splitlines(keepends=True))
        returncode = proc.wait(timeout=10)
        stderr = proc.stderr.read()
    finally:
        os.close(handshake_write_fd)
        proc.stdout.close()
        proc.stderr.close()
        if proc.poll() is None:
            proc.kill()

    assert page1_progress_seen_while_child_alive, (
        "child had already exited before page 1's [PROGRESS] line reached "
        "the parent -- output was buffered until completion, not streamed"
    )
    full_output = "".join(lines)
    assert returncode == 0, f"stdout={full_output!r} stderr={stderr!r}"
    assert "страница=2, карточек=4, деталей=4" in full_output


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
    assert "13 мин-42 мин" in estimate
    assert "ETA уточнится" in estimate

    # A worker now waits before its FIRST request too (#663 Codex review:
    # skipping the delay before "attempts == 0" let every worker burst its
    # first request in lockstep), so even a single detail carries one wait.
    one_detail = _throttle_estimate(
        details=1,
        requested_page_size=1,
        observed_page_size=1,
        min_delay=8,
        max_delay=25,
    )
    assert "троттлинга 8 с-25 с" in one_detail


def test_estimate_accounts_for_parallel_workers():
    estimate = _throttle_estimate(
        details=100,
        requested_page_size=100,
        observed_page_size=100,
        min_delay=8,
        max_delay=25,
        workers=10,
    )

    assert "workers=10" in estimate
    assert "троттлинга 1 мин-4 мин" in estimate


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


def _report_args(tmp_path: Path, **overrides) -> Namespace:
    args = {
        "text": "AI",
        "search_in": None,
        "auth_mode": None,
        "top": 5,
        "history": str(tmp_path / "history.db"),
    }
    args.update(overrides)
    return Namespace(**args)


def _seed_two_scopes(db: Path) -> None:
    from hhru_bot.history import History

    history = History(db)
    for resume_id, role, scope in (
        ("designer", "Графический дизайнер", "full_text"),
        ("engineer", "AI Engineer", "position"),
    ):
        history.upsert_competitor_resume(
            {
                "resume_id": resume_id,
                "resume_url": f"https://hh.ru/resume/{resume_id}",
                "desired_role": role,
                "salary_from": 100_000,
                "salary_to": 150_000,
                "salary_currency": "RUB",
                "experience_months": 48,
                "specializations": ["Разработчик"],
                "employment_types": ["полная занятость"],
                "work_formats": ["удалённо"],
                "languages": ["Русский — Родной"],
                "education": ["Высшее образование"],
                "experience_summary": None,
                "achievements": None,
                "skills": [{"name": "Python", "proficiency": None}],
                "content_hash": f"hash:{resume_id}",
            },
            search_query="AI",
            search_rank=1,
            search_in=scope,
        )


def test_report_scope_flag_excludes_other_search_in(tmp_path, capsys):
    """#669: отчёт по одному --text обязан показывать одну выборку. Без скоупа
    в него попадали дизайнеры из прежнего full_text-прогона."""
    from hhru_bot.commands.competitors import run_report

    db = tmp_path / "history.db"
    _seed_two_scopes(db)

    run_report(_report_args(tmp_path, search_in="position"))
    scoped = capsys.readouterr().out
    assert "AI Engineer" in scoped
    assert "Графический дизайнер" not in scoped

    run_report(_report_args(tmp_path))
    everything = capsys.readouterr().out
    assert "AI Engineer" in everything
    assert "Графический дизайнер" in everything


def test_report_scope_flags_require_text(tmp_path):
    """Область поиска — свойство одной выборки: без --text фильтровать нечего,
    и молча игнорировать флаг нельзя."""
    from hhru_bot.commands.competitors import run_report

    with pytest.raises(ValueError, match="--search-in/--auth-mode"):
        run_report(_report_args(tmp_path, text=None, search_in="position"))

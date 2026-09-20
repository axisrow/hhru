"""bump-live под durable-надзором: ledger, actions, dry-run без записи (#1161).

Тот же контракт, что у боевого bump (test_bump_command_runs.py), но транспорт —
FakeLiveChannel вместо Playwright: боевая семантика (lease, статусы, throttle)
общая, транспортная часть сценария покрыта test_bump_live.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hhru_bot.commands import bump_live as bump_live_command
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.history import History
from hhru_bot.live.scenarios import LiveChannel

pytestmark = pytest.mark.integration


class FakeLiveChannel(LiveChannel):
    """Живой API (start/wait_client/close) со скриптованным сценарием."""

    def __init__(self, port: int = 0, client_timeout: float = 120.0) -> None:
        super().__init__(port=port, client_timeout=client_timeout)
        self.closed = False
        # wait-очередь: список, карточка, hint нет, кнопка есть, маркер
        # появился, кнопка снята (#1184 — добивочное чтение после хинта).
        self.wait_results: list[bool] = [True, True, False, True, True, True]

    def start(self) -> str:
        return "ws://127.0.0.1:0"

    def wait_client(self) -> None:
        return None

    def get_state(self) -> dict:
        return {"url": "https://hh.ru/applicant/resumes"}

    def check(self, selector: str) -> dict:
        return {"found": False}

    def click(self, selector: str, wait_for: dict | None = None, allow_apply: bool = False) -> dict:
        return {"clicked": True, "wait": {"met": True}}

    def wait(self, selector: str, state: str, timeout_ms: int) -> bool:
        return self.wait_results.pop(0)

    def close(self) -> None:
        self.closed = True


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="letter",
        resumes=[
            ResumeConfig(
                id="resume",
                resume_url="https://hh.ru/resume/abc123",
                search=SearchFilters(text="python", area=1),
            )
        ],
    )


def _args(tmp_path: Path, *, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        command="bump-live",
        config=None,
        history=str(tmp_path / "history.db"),
        dry_run=dry_run,
        resume=None,
        max_pages=5,
        port=0,
    )


def _patch_runtime(monkeypatch, config: AppConfig) -> None:
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.throttle.Throttle.wait", lambda *_a, **_kw: None)
    monkeypatch.setattr("hhru_bot.live.scenarios.LiveChannel", FakeLiveChannel)


def test_bump_live_persists_action_and_run(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)

    assert bump_live_command.run(_args(tmp_path)) is False

    history = History(tmp_path / "history.db")
    row = history.command_runs()[-1]
    assert row["command"] == "bump-live"
    assert (row["status"], row["attempted"], row["success"], row["failed"]) == (
        "completed",
        1,
        1,
        0,
    )
    with history._connect() as conn:
        action = conn.execute(
            "SELECT status, action, run_id FROM actions WHERE action='bump'"
        ).fetchone()
    # Тот же action-имя 'bump', что у боевого пути — cooldown/лимиты видят оба.
    assert action["status"] == "success"
    assert action["run_id"] == row["run_id"]
    out = capsys.readouterr().out
    assert "[RUN]" in out
    assert "[OK]" in out


def test_bump_live_dry_run_writes_no_action(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)

    assert bump_live_command.run(_args(tmp_path, dry_run=True)) is False

    history = History(tmp_path / "history.db")
    row = history.command_runs()[-1]
    assert row["status"] == "completed"
    with history._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert "[OK]" in capsys.readouterr().out


def test_bump_live_closes_channel_after_run(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)

    created: list[FakeLiveChannel] = []
    original_init = FakeLiveChannel.__init__

    def _tracking_init(self, *args, **kwargs) -> None:  # noqa: ANN001
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(FakeLiveChannel, "__init__", _tracking_init)
    assert bump_live_command.run(_args(tmp_path)) is False
    assert created and created[0].closed

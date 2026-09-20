"""apply-live под durable-надзором: ledger, actions, dry-run без записи (#1162).

Тот же контракт, что у боевого apply (apply_service._execute_apply_wave):
lease, begin_action/finalize_action, record_skip, throttle после acted.
Playwright-часть (identity, verify, карточка) подменяется стабами — серая зона
покрыта test_apply_live.py; здесь — проводка команды.
"""

from __future__ import annotations

import argparse
import contextlib
from pathlib import Path

import pytest

from hhru_bot.commands import apply_live as apply_live_command
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.history import History
from hhru_bot.live.scenarios import LiveChannel

pytestmark = pytest.mark.integration

VACANCY_ID = "136789544"
RESUME_ID = "0123abcd"


class FakeLiveChannel(LiveChannel):
    """Живой API (start/wait_client/close) с фиктивной формой: клики идут,
    маркеры успеха появляются — сценарий обязан завершиться success."""

    def __init__(self, port: int = 0, client_timeout: float = 120.0) -> None:
        super().__init__(port=port, client_timeout=client_timeout)
        self.closed = False

    def start(self) -> str:
        return "ws://127.0.0.1:8765"

    def wait_client(self) -> None:
        return None

    def get_state(self) -> dict:
        return {"url": f"https://hh.ru/vacancy/{VACANCY_ID}"}

    def check(self, selector: str) -> dict:
        # Форма готова: пикер и панель на месте (мульти-резюме аккаунт), всё
        # прочее не найдено — сценарию достаточно.
        if (
            "resume-title" in selector
            or "drop-base" in selector
            or "magritte-select-option-" in selector
        ):
            return {"found": True, "visible": True, "matchCount": 1, "text": ""}
        return {"found": False, "visible": False, "matchCount": 0, "text": ""}

    def click_wait_met(self, selector: str, wait_for: dict, allow_apply: bool = False) -> bool:
        return True

    def wait(self, selector: str, state: str, timeout_ms: int) -> bool:
        return True

    def fill(self, selector: str, text: str) -> dict:
        return {"filled": True, "length": len(text)}

    def close(self) -> None:
        self.closed = True


class _FakePage:
    def close(self) -> None:
        pass


@contextlib.contextmanager
def _fake_launch_context(*_a, **_kw):
    yield type("Ctx", (), {"new_page": staticmethod(lambda: _FakePage())})()


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        storage_state_file=tmp_path / "state.json",
        throttle=ThrottleConfig(),
        cover_letter_default="Привет, {vacancy_title}!",
        resumes=[
            ResumeConfig(
                id="resume",
                resume_url=f"https://hh.ru/resume/{RESUME_ID}",
                search=SearchFilters(text="python", area=1),
            )
        ],
    )


def _args(tmp_path: Path, *, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        command="apply-live",
        config=None,
        history=str(tmp_path / "history.db"),
        dry_run=dry_run,
        resume="resume",
        vacancy=f"https://hh.ru/vacancy/{VACANCY_ID}",
        max_pages=5,
        port=8765,
        headless=True,
    )


def _patch_runtime(monkeypatch, config: AppConfig, *, verdict: str = "found") -> None:
    from hhru_bot.apply.verify import NegotiationsVerifyResult

    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.throttle.Throttle.wait", lambda *_a, **_kw: None)
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr("hhru_bot.browser.require_authenticated_session", lambda _page: None)
    monkeypatch.setattr("hhru_bot.apply.antibot.raise_for_antibot", lambda _page: None)

    from hhru_bot.commands.apply_service import ApplyResumeIdentity
    from hhru_bot.search import VacancyCard

    monkeypatch.setattr(
        "hhru_bot.commands.apply_service._prepare_apply_resume",
        lambda _page, _resume, _dry: ApplyResumeIdentity(
            RESUME_ID, {RESUME_ID, "999fed"}, {RESUME_ID, "999fed"}
        ),
    )
    monkeypatch.setattr(
        "hhru_bot.search.fetch_vacancy_card",
        lambda _page, _vid: VacancyCard(
            vacancy_id=VACANCY_ID,
            title="Инженер",
            company="YADRO",
            url=f"https://hh.ru/vacancy/{VACANCY_ID}",
        ),
    )
    monkeypatch.setattr("hhru_bot.blacklist.match", lambda _card, _sets: None)
    monkeypatch.setattr("hhru_bot.search.current_employer_hit", lambda _c, _e: None)

    def _fake_verify(*_a, **_kw) -> NegotiationsVerifyResult:
        return NegotiationsVerifyResult(verdict, "topic=1")

    monkeypatch.setattr("hhru_bot.apply.verify.verify_response_in_negotiations", _fake_verify)
    monkeypatch.setattr("hhru_bot.live.scenarios.LiveChannel", FakeLiveChannel)


def test_apply_live_success_records_action_and_run(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config, verdict="found")

    assert apply_live_command.run(_args(tmp_path)) is False

    history = History(tmp_path / "history.db")
    row = history.command_runs()[-1]
    assert row["command"] == "apply-live"
    assert (row["status"], row["attempted"], row["success"]) == ("completed", 1, 1)
    with history._connect() as conn:
        action = conn.execute(
            "SELECT status, action, run_id, vacancy_id FROM actions WHERE action='apply'"
        ).fetchone()
    # Тот же action-имя 'apply', что у боевого пути — has_applied/лимиты видят оба.
    assert (action["status"], action["vacancy_id"]) == ("success", VACANCY_ID)
    assert action["run_id"] == row["run_id"]
    out = capsys.readouterr().out
    assert "[RUN]" in out and "[OK]" in out


def test_apply_live_dry_run_writes_no_action(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)

    assert apply_live_command.run(_args(tmp_path, dry_run=True)) is False

    history = History(tmp_path / "history.db")
    with history._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_apply_live_dedup_bars_second_apply(tmp_path, monkeypatch, capsys) -> None:
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)
    history = History(tmp_path / "history.db")
    history.record_action(RESUME_ID, VACANCY_ID, "apply", "success", "боевой отклик ранее")

    assert apply_live_command.run(_args(tmp_path)) is False

    out = capsys.readouterr().out
    assert "[skip]" in out and "уже есть в истории" in out


def test_apply_live_uncertain_grey_zone_without_confirmation(tmp_path, monkeypatch) -> None:
    # Маркер успеха после submit не подтвердился, верификатор недоступен
    # (indeterminate): fail-closed uncertain+acted — has_applied увидит запись,
    # дневной лимит израсходован честно.
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config, verdict="indeterminate")

    class _NoMarkerChannel(FakeLiveChannel):
        def click_wait_met(self, selector: str, wait_for: dict, allow_apply: bool = False) -> bool:
            return "vacancy-response-submit" not in selector

    monkeypatch.setattr("hhru_bot.live.scenarios.LiveChannel", _NoMarkerChannel)

    assert apply_live_command.run(_args(tmp_path)) is True

    history = History(tmp_path / "history.db")
    row = history.command_runs()[-1]
    # attempted>0 + провал = partial (та же формула run_supervised_command).
    assert (row["status"], row["uncertain"]) == ("partial", 1)
    with history._connect() as conn:
        action = conn.execute("SELECT status FROM actions WHERE action='apply'").fetchone()
    assert action["status"] == "uncertain"


def test_apply_live_questions_queue_on_skip(tmp_path, monkeypatch) -> None:
    # Анкета: вакансия skip, вопросы — в очередь обучения; канал не отвечает сам.
    config = _config(tmp_path)
    _patch_runtime(monkeypatch, config)

    class _QuestionsChannel(FakeLiveChannel):
        def check(self, selector: str) -> dict:
            if "task-body" in selector:
                return {"found": True, "visible": True, "matchCount": 1, "text": ""}
            if "task-question" in selector:
                return {"found": True, "visible": True, "matchCount": 1, "text": "Опыт с LLM?"}
            return {"found": False, "visible": False, "matchCount": 0, "text": ""}

    monkeypatch.setattr("hhru_bot.live.scenarios.LiveChannel", _QuestionsChannel)

    assert apply_live_command.run(_args(tmp_path)) is False

    history = History(tmp_path / "history.db")
    with history._connect() as conn:
        pending = conn.execute("SELECT question_text FROM questionnaire_pending").fetchall()
        skipped = conn.execute("SELECT reason FROM skipped").fetchone()
    assert [row["question_text"] for row in pending] == ["Опыт с LLM?"]
    assert skipped is not None

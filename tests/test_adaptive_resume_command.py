"""CLI-тесты команды adaptive-resume (#753/#769): резолв резюме, fail-closed
без ai, и --apply (#769: применение title/about/skills на hh.ru)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.commands.adaptive_resume as command
from hhru_bot.adaptive_resume_apply import StepResult
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.config_sections.candidate_facts import CandidateFacts, WorkExperienceFact
from hhru_bot.history import History

pytestmark = pytest.mark.unit


def _facts() -> CandidateFacts:
    return CandidateFacts(
        work_experience=[
            WorkExperienceFact(
                company="ООО Данные",
                position="Backend-разработчик",
                period_from="2021-03",
                period_to="2024-06",
                description="Разработка API на Django.",
                skills=["python", "django"],
                tags=["backend", "python"],
            )
        ]
    )


def _config(*, with_facts: bool = True) -> AppConfig:
    resume = ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/AAA111",
        search=SearchFilters(text=""),
        candidate_facts=_facts() if with_facts else None,
    )
    return AppConfig(
        storage_state_file="unused",
        throttle=ThrottleConfig(),
        cover_letter_default="",
        resumes=[resume],
        ai=None,
    )


def test_missing_candidate_facts_section_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit", lambda _path: _config(with_facts=False)
    )
    args = SimpleNamespace(config="unused", resume="r1", cluster="python_backend", dry_run=True)

    with pytest.raises(SystemExit) as exc:
        command.run(args)

    assert exc.value.code == 1
    assert "candidate_facts" in capsys.readouterr().out


def test_unknown_resume_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit", lambda _path: _config(with_facts=True)
    )
    args = SimpleNamespace(
        config="unused", resume="does-not-exist", cluster="python_backend", dry_run=True
    )

    with pytest.raises(SystemExit) as exc:
        command.run(args)

    assert exc.value.code == 1


def test_dry_run_without_ai_uses_fallback_and_prints_plan(monkeypatch, capsys):
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit", lambda _path: _config(with_facts=True)
    )
    args = SimpleNamespace(config="unused", resume="r1", cluster="python_backend", dry_run=True)

    command.run(args)

    output = capsys.readouterr().out
    assert "[DRY-RUN]" in output
    assert "fallback" in output
    assert "на hh.ru не сохраняются" in output
    assert "Django" in output or "django" in output.lower()
    # cycle-review round 1 (/review): вывод обязан показывать читаемое имя
    # кластера ("Python-бэкенд"), а не внутренний слаг ("python_backend").
    assert "Python-бэкенд" in output
    assert "python_backend»" not in output


def _apply_args(*, dry_run: bool, force: bool = False, history: str) -> SimpleNamespace:
    return SimpleNamespace(
        config="unused",
        resume="r1",
        cluster="python_backend",
        apply=True,
        dry_run=dry_run,
        force=force,
        history=history,
        headless=True,
    )


@contextmanager
def _fake_context():
    yield SimpleNamespace(new_page=lambda: object())


def test_apply_dry_run_prints_each_step_and_does_not_confirm(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit", lambda _path: _config(with_facts=True)
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **k: _fake_context())
    monkeypatch.setattr(
        "hhru_bot.adaptive_resume_apply.apply_adaptive_resume",
        lambda page, resume, content, *, dry_run: (
            StepResult("title", success=True, reason="предложено"),
            StepResult("about", success=True, reason="предложено"),
            StepResult("skills", skipped=True, reason="кластер не предложил навыков"),
        ),
    )

    args = _apply_args(dry_run=True, history=str(tmp_path / "history.db"))
    failed = command.run(args)

    output = capsys.readouterr().out
    assert "[DRY-RUN] title: предложено" in output
    assert "[DRY-RUN] about: предложено" in output
    assert "[skip] skills:" in output
    assert not failed


def test_apply_without_force_or_tty_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit", lambda _path: _config(with_facts=True)
    )
    monkeypatch.setattr("hhru_bot.commands.copy_resume.confirm_write", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(
        "hhru_bot.browser.launch_context",
        lambda *a, **k: called.append(True) or _fake_context(),
    )

    args = _apply_args(dry_run=False, force=False, history=str(tmp_path / "history.db"))
    failed = command.run(args)

    assert failed
    assert not called  # browser must never open without confirmation


def test_apply_force_records_action_and_reports_per_step(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit", lambda _path: _config(with_facts=True)
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **k: _fake_context())
    monkeypatch.setattr(
        "hhru_bot.adaptive_resume_apply.apply_adaptive_resume",
        lambda page, resume, content, *, dry_run: (
            StepResult("title", success=True, acted=True, reason="сохранён"),
            StepResult("about", success=True, acted=True, reason="сохранён"),
            StepResult("skills", uncertain=True, acted=True, reason="не подтверждено"),
        ),
    )

    history_path = str(tmp_path / "history.db")
    args = _apply_args(dry_run=False, force=True, history=history_path)
    failed = command.run(args)

    assert failed  # one uncertain step marks the whole command failed
    rows = History(history_path).list_actions(None, "all")
    assert any(row["action"] == "adaptive_resume_apply" for row in rows)
    assert any(row["status"] == "uncertain" for row in rows)

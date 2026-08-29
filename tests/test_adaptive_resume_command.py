"""CLI-тесты команды adaptive-resume (#753): резолв резюме, fail-closed без ai."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hhru_bot.commands.adaptive_resume as command
from hhru_bot.config import AppConfig, ResumeConfig, SearchFilters, ThrottleConfig
from hhru_bot.config_sections.candidate_facts import CandidateFacts, WorkExperienceFact

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
    assert "Ничего не сохранено на hh.ru" in output
    assert "Django" in output or "django" in output.lower()

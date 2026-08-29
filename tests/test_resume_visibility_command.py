"""Safety contract for resume-visibility (#566, #746)."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hhru_bot.browser
import hhru_bot.commands.resume_visibility as cmd
import hhru_bot.resume_visibility as rv
from hhru_bot.cli import build_parser
from hhru_bot.history import History

pytestmark = pytest.mark.integration

RESUME_ID = "a" * 38
OTHER_RESUME_ID = "b" * 38


def _resume(slug="python", resume_id=RESUME_ID):
    return SimpleNamespace(id=slug, resume_id=resume_id)


def _config(tmp_path, resumes=None):
    resumes = resumes if resumes is not None else [_resume()]
    by_id = {r.id: r for r in resumes}

    def get_resume(value):
        if value in by_id:
            return by_id[value]
        raise KeyError(value)

    return SimpleNamespace(
        get_resume=get_resume,
        resumes=resumes,
        storage_state_file=tmp_path / "session.json",
        user_agent=None,
    )


def _args(tmp_path, **overrides):
    values = dict(
        config="unused.yaml",
        history=str(tmp_path / "h.db"),
        headless=True,
        resume="python",
        mode="link-only",
        add_employer=[],
        remove_employer=[],
        dry_run=False,
        force=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_parser_exposes_all_explicit_modes():
    parser = build_parser()
    args = parser.parse_args(["resume-visibility", "--resume", "x", "--mode", "whitelist"])
    assert args.mode == "whitelist"


def test_parser_accepts_repeated_employer_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "resume-visibility",
            "--resume",
            "all",
            "--mode",
            "blacklist",
            "--add-employer",
            "Ксамата",
            "--add-employer",
            "Law Business Group",
        ]
    )
    assert args.add_employer == ["Ксамата", "Law Business Group"]


def test_no_mode_and_no_employer_flags_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path))
    assert cmd.run(_args(tmp_path, mode=None)) is True


def test_dry_run_is_preview_and_writes_no_action(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path))
    monkeypatch.setattr(hhru_bot.browser, "launch_context", lambda *a, **kw: _context())
    assert cmd.run(_args(tmp_path, dry_run=True)) is False
    assert "DRY-RUN" in capsys.readouterr().out
    assert History(tmp_path / "h.db").count_today(RESUME_ID, "resume_visibility") == 0


def test_live_write_succeeds_with_confirmed_selectors(tmp_path, monkeypatch, capsys):
    """Selectors are confirmed as of #746 — a boxed real click path now runs."""
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path))
    monkeypatch.setattr(hhru_bot.browser, "launch_context", lambda *a, **kw: _context())
    monkeypatch.setattr(rv, "goto_hh", lambda *_a, **_kw: None)

    save = MagicMock()
    save.count.return_value = 1
    save.first = save
    mode_label = MagicMock()
    mode_label.count.return_value = 1
    mode_label.first = mode_label

    def fake_set_visibility(page, resume, mode, dry_run, *, before_click=None, **kwargs):
        del page, mode, dry_run, kwargs
        if before_click is not None:
            before_click()
        return rv.ResumeVisibilityResult(resume.resume_id, True, "видимость сохранена")

    monkeypatch.setattr(rv, "set_resume_visibility_on_hh", fake_set_visibility)

    assert cmd.run(_args(tmp_path, force=True)) is False
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert History(tmp_path / "h.db").count_today(RESUME_ID, "resume_visibility") == 1


def test_resume_all_iterates_every_configured_resume(tmp_path, monkeypatch, capsys):
    resumes = [_resume("python", RESUME_ID), _resume("marketing", OTHER_RESUME_ID)]
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path, resumes)
    )
    monkeypatch.setattr(hhru_bot.browser, "launch_context", lambda *a, **kw: _context())

    calls = []

    def fake_set_visibility(page, resume, mode, dry_run, **kwargs):
        del page, kwargs
        calls.append(resume.resume_id)
        return rv.ResumeVisibilityResult(resume.resume_id, True, f"dry-run; режим -> «{mode}»")

    monkeypatch.setattr(rv, "set_resume_visibility_on_hh", fake_set_visibility)

    assert cmd.run(_args(tmp_path, resume="all", dry_run=True)) is False
    assert calls == [RESUME_ID, OTHER_RESUME_ID]
    out = capsys.readouterr().out
    assert out.count("[DRY-RUN]") == 2


def test_resume_all_skips_only_blocked_resume_not_whole_batch(tmp_path, monkeypatch, capsys):
    """Regression for #746 review round 3: has_unresolved_uncertain must not
    abort the whole --resume all batch for one blocked resume_id — it should
    exclude that resume and still process the rest (matches the per-resume
    [OK]/[FAIL] reporting _body already uses)."""
    resumes = [_resume("python", RESUME_ID), _resume("marketing", OTHER_RESUME_ID)]
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path, resumes)
    )
    monkeypatch.setattr(hhru_bot.browser, "launch_context", lambda *a, **kw: _context())

    history = History(tmp_path / "h.db")
    history.begin_action(RESUME_ID, RESUME_ID, "resume_visibility")  # leaves it "uncertain"

    calls = []

    def fake_set_visibility(page, resume, mode, dry_run, *, before_click=None, **kwargs):
        del page, mode, dry_run, kwargs
        calls.append(resume.resume_id)
        if before_click is not None:
            before_click()
        return rv.ResumeVisibilityResult(resume.resume_id, True, "видимость сохранена")

    monkeypatch.setattr(rv, "set_resume_visibility_on_hh", fake_set_visibility)

    result = cmd.run(_args(tmp_path, resume="all", force=True))

    # OTHER_RESUME_ID (not blocked) was processed; RESUME_ID (blocked) was not.
    assert calls == [OTHER_RESUME_ID]
    out = capsys.readouterr().out
    assert "не подтверждено (uncertain)" in out
    assert "[OK]" in out
    # Partial success: one resume was blocked, so the run is not a clean success.
    assert result is True


def test_employer_flags_reject_incompatible_explicit_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path))
    result = cmd.run(_args(tmp_path, mode="everyone", add_employer=["Ксамата"], dry_run=True))
    assert result is True


def test_ambiguous_employer_match_is_reported_not_guessed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda path: _config(tmp_path))
    monkeypatch.setattr(hhru_bot.browser, "launch_context", lambda *a, **kw: _context())

    candidates = [
        rv.EmployerCandidate(employer_id="3529", name="СБЕР", city="Москва"),
        rv.EmployerCandidate(employer_id="9001", name="Сбер Банк", city="Минск"),
    ]

    def fake_set_visibility(page, resume, mode, dry_run, **kwargs):
        del page, mode, dry_run, kwargs
        return rv.ResumeVisibilityResult(
            resume.resume_id,
            False,
            "найдено 2 работодателей с именем «Сбер» — уточните",
            ambiguous_candidates=candidates,
            ambiguous_query="Сбер",
        )

    monkeypatch.setattr(rv, "set_resume_visibility_on_hh", fake_set_visibility)

    assert cmd.run(_args(tmp_path, mode=None, add_employer=["Сбер"], force=True)) is True
    out = capsys.readouterr().out
    assert "СБЕР" in out
    assert "Сбер Банк" in out
    assert "employer_id=3529" in out
    assert "employer_id=9001" in out


class _Context:
    def __enter__(self):
        return SimpleNamespace(new_page=lambda: SimpleNamespace())

    def __exit__(self, *exc):
        return False


def _context():
    return _Context()

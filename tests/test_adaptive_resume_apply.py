"""Unit tests for adaptive_resume_apply.py (#769): title/about/skills orchestration.

Each underlying WRITE path (resume_position/about/skills) is monkeypatched at
the module boundary adaptive_resume_apply imports from — these tests verify
the orchestration (independent per-step reporting, dry-run short-circuiting,
uncertain propagation), not the browser mechanics those modules already own
and test themselves.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hhru_bot import adaptive_resume_apply as apply_mod
from hhru_bot.adaptive_resume import AdaptiveResumeContent
from hhru_bot.config import ResumeConfig, SearchFilters

pytestmark = pytest.mark.unit


def _resume() -> ResumeConfig:
    return ResumeConfig(
        id="r1",
        resume_url="https://hh.ru/resume/00001aaa",
        search=SearchFilters(text=""),
    )


def _content(**overrides) -> AdaptiveResumeContent:
    defaults = dict(
        cluster_key="python_backend",
        title="Python-разработчик",
        about="Пишу на Python.",
        skills=("Python", "Django"),
        work_experience=(),
        projects=(),
        source="fallback",
    )
    defaults.update(overrides)
    return AdaptiveResumeContent(**defaults)


class _FakePage:
    """Minimal Page double — only .locator(...).click()/.wait_for() used."""

    class _Locator:
        def click(self):
            return None

        def wait_for(self, *, state=None, timeout=None):
            return None

        def count(self):
            return 1

    def locator(self, _selector):
        return self._Locator()


def test_apply_title_skips_when_already_matching(monkeypatch):
    flow = SimpleNamespace(kind="editor", values=SimpleNamespace(title="Python-разработчик"))
    monkeypatch.setattr(apply_mod, "_apply_title", apply_mod._apply_title)
    monkeypatch.setattr("hhru_bot.resume_position.open_position_form", lambda page, resume: flow)

    result = apply_mod._apply_title(_FakePage(), _resume(), "Python-разработчик", dry_run=True)

    assert result.success
    assert "уже совпадает" in result.reason


def test_apply_title_fails_closed_on_wizard(monkeypatch):
    flow = SimpleNamespace(kind="wizard", values=SimpleNamespace(title=""))
    monkeypatch.setattr("hhru_bot.resume_position.open_position_form", lambda page, resume: flow)

    result = apply_mod._apply_title(_FakePage(), _resume(), "Python-разработчик", dry_run=True)

    assert not result.success
    assert not result.acted
    assert "визард" in result.reason


def test_apply_title_dry_run_cancels_without_save(monkeypatch):
    flow = SimpleNamespace(kind="editor", values=SimpleNamespace(title="Старый заголовок"))
    monkeypatch.setattr("hhru_bot.resume_position.open_position_form", lambda page, resume: flow)
    monkeypatch.setattr("hhru_bot.resume_position.apply_position", lambda page, plan, current: None)

    result = apply_mod._apply_title(_FakePage(), _resume(), "Новый заголовок", dry_run=True)

    assert result.success
    assert not result.acted
    assert "save не нажат" in result.reason


def test_apply_title_uncertain_when_save_confirmation_fails(monkeypatch):
    from playwright.sync_api import Error as PlaywrightError

    flow = SimpleNamespace(kind="editor", values=SimpleNamespace(title="Старый заголовок"))
    monkeypatch.setattr("hhru_bot.resume_position.open_position_form", lambda page, resume: flow)
    monkeypatch.setattr("hhru_bot.resume_position.apply_position", lambda page, plan, current: None)

    class _FlakyPage(_FakePage):
        class _Locator(_FakePage._Locator):
            def wait_for(self, *, state=None, timeout=None):
                raise PlaywrightError("timeout")

        def locator(self, _selector):
            return self._Locator()

    result = apply_mod._apply_title(_FlakyPage(), _resume(), "Новый заголовок", dry_run=False)

    assert not result.success
    assert result.uncertain
    assert result.acted


def test_apply_about_skips_when_text_matches(monkeypatch):
    monkeypatch.setattr("hhru_bot.about.open_about_editor", lambda page, resume: "Пишу на Python.")

    result = apply_mod._apply_about(_FakePage(), _resume(), "Пишу на Python.", dry_run=False)

    assert result.success
    assert not result.acted
    assert "уже совпадает" in result.reason


def test_apply_about_dry_run_does_not_call_save(monkeypatch):
    monkeypatch.setattr("hhru_bot.about.open_about_editor", lambda page, resume: "Старый текст")
    calls = []
    monkeypatch.setattr("hhru_bot.about.save_about", lambda page, text: calls.append(text))

    result = apply_mod._apply_about(_FakePage(), _resume(), "Новый текст", dry_run=True)

    assert result.success
    assert not calls


def test_apply_about_uncertain_propagates_from_save(monkeypatch):
    from hhru_bot.about import AboutGenerationError

    monkeypatch.setattr("hhru_bot.about.open_about_editor", lambda page, resume: "Старый текст")

    def _raise(page, text):
        raise AboutGenerationError("сохранение не подтверждено (uncertain): тест")

    monkeypatch.setattr("hhru_bot.about.save_about", _raise)

    result = apply_mod._apply_about(_FakePage(), _resume(), "Новый текст", dry_run=False)

    assert not result.success
    assert result.uncertain
    assert result.acted


def test_apply_skills_skips_when_no_skills_proposed():
    result = apply_mod._apply_skills(_FakePage(), _resume(), (), dry_run=False)

    assert result.skipped
    assert not result.success


def test_apply_skills_reports_added_names(monkeypatch):
    from hhru_bot.skills import SkillsResult

    monkeypatch.setattr(
        "hhru_bot.skills.edit_skills_on_hh",
        lambda page, resume, skills, *, dry_run, mode: SkillsResult(
            success=True, added=("Python", "Django")
        ),
    )

    result = apply_mod._apply_skills(_FakePage(), _resume(), ("Python", "Django"), dry_run=False)

    assert result.success
    assert result.acted
    assert "Python" in result.reason and "Django" in result.reason


def test_apply_adaptive_resume_runs_all_three_steps_independently(monkeypatch):
    """A failing title step must not prevent about/skills from being attempted."""
    monkeypatch.setattr(
        apply_mod,
        "_apply_title",
        lambda page, resume, title, *, dry_run: apply_mod.StepResult("title", reason="boom"),
    )
    monkeypatch.setattr(
        apply_mod,
        "_apply_about",
        lambda page, resume, about, *, dry_run: apply_mod.StepResult("about", success=True),
    )
    monkeypatch.setattr(
        apply_mod,
        "_apply_skills",
        lambda page, resume, skills, *, dry_run: apply_mod.StepResult("skills", success=True),
    )

    results = apply_mod.apply_adaptive_resume(_FakePage(), _resume(), _content(), dry_run=True)

    assert [r.step for r in results] == ["title", "about", "skills"]
    assert not results[0].success
    assert results[1].success
    assert results[2].success

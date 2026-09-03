"""Командная проводка resume-position: дубль-гард должности (#911).

Должности в аккаунте уникальны (живая проверка пользователя 2026-09-01):
дубликат 1 в 1 молча не сохраняется. Команда обязана отклонять его ДО
клика сохранения и до запроса подтверждения — после клика отказ hh.ru
невидим. Здесь проверяется проводка на двойниках; чистая логика гарда — в
test_resume_titles.py, browser-слой визарда — в test_resume_position.py.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import hhru_bot.browser
import hhru_bot.commands.resume_position as cmd
import hhru_bot.resume_position
from hhru_bot.resume_position import PositionValues

pytestmark = pytest.mark.integration

RESUME_ID = "c" * 38


def _args(tmp_path, **overrides):
    base = {
        "config": "unused.yaml",
        "history": str(tmp_path / "h.db"),
        "headless": True,
        "resume": "draft",
        "dry_run": False,
        "force": True,
        "title": "Программист",
        "specialization": None,
        "salary": None,
        "currency": None,
        "employment": None,
        "work_format": None,
        "commute": None,
        "business_trips": None,
        "mode": None,
        "command": "resume-position",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def env(monkeypatch, tmp_path):
    resume = SimpleNamespace(id="draft", resume_id=RESUME_ID, ai_profile=None)

    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda path: SimpleNamespace(
            get_resume=lambda rid: resume,
            storage_state_file=tmp_path / "s.json",
            user_agent=None,
            ai=None,
        ),
    )

    clicks: list[str] = []

    class FakePage:
        def locator(self, selector):
            return SimpleNamespace(click=lambda: clicks.append(selector))

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    @contextmanager
    def fake_launch(*a, **kw):
        yield FakeContext()

    monkeypatch.setattr(hhru_bot.browser, "launch_context", fake_launch)

    # Editor-режим с текущим заголовком «QA»: ручной --title без LLM.
    flow = SimpleNamespace(
        kind="editor",
        resume_id=RESUME_ID,
        values=PositionValues(title="QA"),
        state=SimpleNamespace(next_incomplete_screen_id=None),
    )
    monkeypatch.setattr(
        hhru_bot.resume_position,
        "open_position_form",
        lambda page, resume, enter_wizard=True: flow,
    )
    return SimpleNamespace(clicks=clicks)


def test_duplicate_title_refused_before_save(env, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hhru_bot.resume_titles.account_duplicate_reason",
        lambda page, title, exclude_resume_id="": (
            "резюме с должностью «Программист» уже существует; "
            "должности в аккаунте уникальны, запись запрещена"
        ),
    )

    def no_save(*a, **kw):
        pytest.fail("сохранение не должно выполняться при дубликате должности")

    monkeypatch.setattr(hhru_bot.resume_position, "apply_position", no_save)

    assert cmd.run(_args(tmp_path)) is True

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "уже существует" in out
    # Editor-форма закрыта штатной отменой — та же конвенция, что у остальных
    # отказов editor-пути команды.
    assert env.clicks


def test_guard_passes_target_and_own_resume_identity(env, capsys, tmp_path, monkeypatch):
    seen: dict[str, str] = {}

    def _spy(page, title, exclude_resume_id=""):
        seen["title"] = title
        seen["exclude"] = exclude_resume_id
        return ""

    monkeypatch.setattr("hhru_bot.resume_titles.account_duplicate_reason", _spy)
    monkeypatch.setattr(
        hhru_bot.resume_position,
        "apply_position",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(cmd, "_click_save_and_wait", lambda page: None)

    assert cmd.run(_args(tmp_path)) is False

    # Гарда получает целевую должность и исключает само редактируемое резюме:
    # сохранить должность, которую оно уже носит, — не дубль.
    assert seen == {"title": "Программист", "exclude": RESUME_ID}
    assert "[OK] Раздел желаемой работы резюме 'draft' обновлён." in capsys.readouterr().out


def test_no_title_change_skips_guard(env, capsys, tmp_path, monkeypatch):
    """План без изменения title ничего не пишет в должность — гарда не нужна."""
    asked: list[str] = []

    def _spy(page, title, exclude_resume_id=""):
        asked.append(title)
        return ""

    monkeypatch.setattr("hhru_bot.resume_titles.account_duplicate_reason", _spy)
    monkeypatch.setattr(
        hhru_bot.resume_position,
        "apply_position",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(cmd, "_click_save_and_wait", lambda page: None)

    assert cmd.run(_args(tmp_path, title=None, salary=200000)) is False

    assert asked == []
    assert "[OK] Раздел желаемой работы резюме 'draft' обновлён." in capsys.readouterr().out


def test_editor_dry_run_without_title_prints_current_title(env, capsys, tmp_path):
    """#910: editor-путь без --title терял заголовок черновика (title: None).

    Печать показывает фактический заголовок, но сам план не меняется:
    title=None по-прежнему значит «не трогать», и дубль-гард #911 не
    вызывается (см. test_no_title_change_skips_guard).
    """
    assert (
        cmd.run(
            _args(tmp_path, title=None, specialization=["Инженер по тестированию"], dry_run=True)
        )
        is False
    )

    out = capsys.readouterr().out
    assert "title: QA" in out
    assert "[INFO] Ничего не записано на hh.ru." in out


def test_editor_dry_run_without_title_keeps_honest_none(env, capsys, tmp_path, monkeypatch):
    """#910: черновик без заголовка печатает честный None, а не догадку."""
    flow = SimpleNamespace(
        kind="editor",
        resume_id=RESUME_ID,
        values=PositionValues(title=None),
        state=SimpleNamespace(next_incomplete_screen_id=None),
    )
    monkeypatch.setattr(
        hhru_bot.resume_position,
        "open_position_form",
        lambda page, resume, enter_wizard=True: flow,
    )

    assert (
        cmd.run(
            _args(tmp_path, title=None, specialization=["Инженер по тестированию"], dry_run=True)
        )
        is False
    )

    out = capsys.readouterr().out
    assert "title: None" in out
    assert "[INFO] Ничего не записано на hh.ru." in out

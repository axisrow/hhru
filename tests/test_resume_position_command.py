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
        def __init__(self):
            self._navigated = False

        def mark_navigated(self):
            # #963 follow-up: goto_hh уводит с формы редактора — CANCEL на
            # целевой странице не существует, как и на живом DOM.
            self._navigated = True

        def locator(self, selector):
            if self._navigated and selector == hhru_bot.resume_position.CANCEL:
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

                def click():
                    raise PlaywrightTimeoutError("CANCEL vanished after navigation")

                return SimpleNamespace(click=click)
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

    # #950: dry-run editor-пути валидирует --specialization по живому дереву —
    # на двойнике проход; refusal-сценарии переопределяют в тестах ниже.
    validated: list[list[str]] = []
    monkeypatch.setattr(
        hhru_bot.resume_position,
        "validate_specializations_against_tree",
        lambda page, values: (validated.append(list(values)), [])[1],
    )
    monkeypatch.setattr(hhru_bot.browser, "goto_hh", lambda page, url: None)

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
    return SimpleNamespace(clicks=clicks, validated=validated)


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


def test_editor_dry_run_validates_specializations_against_tree(env, capsys, tmp_path):
    """#950: dry-run editor-пути сверяет --specialization с живым деревом."""
    assert (
        cmd.run(
            _args(tmp_path, title=None, specialization=["Инженер по тестированию"], dry_run=True)
        )
        is False
    )

    assert env.validated == [["Инженер по тестированию"]]
    assert "[INFO] Ничего не записано на hh.ru." in capsys.readouterr().out


def test_editor_dry_run_with_specialization_skips_cancel_after_navigation(
    env, capsys, tmp_path, monkeypatch
):
    """#963 follow-up: dry-run со spec-валидацией уходит с формы через goto_hh
    (панель закрывается навигацией), после чего CANCEL на /resume/{id} не
    существует — клик по нему валил бы dry-run по таймауту после УСПЕШНОЙ
    сверки. Фейковая страница отражает живой DOM: после навигации клик по
    CANCEL падает — команда обязана его не делать."""
    monkeypatch.setattr(
        hhru_bot.browser,
        "goto_hh",
        lambda page, url: page.mark_navigated(),
    )
    # Панель сверена, отказов нет: успешный dry-run обязан закончиться
    # [INFO] без клика по исчезнувшей после навигации CANCEL.
    assert (
        cmd.run(
            _args(tmp_path, title=None, specialization=["Инженер по тестированию"], dry_run=True)
        )
        is False
    )

    assert "[INFO] Ничего не записано на hh.ru." in capsys.readouterr().out


def test_editor_dry_run_refuses_missing_specialization_before_any_click(
    env, capsys, tmp_path, monkeypatch
):
    """Отказ перечисляет кандидатов и не доходит ни до CANCEL, ни до записи."""
    monkeypatch.setattr(
        hhru_bot.resume_position,
        "validate_specializations_against_tree",
        lambda page, values: [
            "специализация не найдена в дереве резюме (dry-run сверка до записи, "
            "#950): Врач-хирург; ближайшие доступные листы: Врач"
        ],
    )

    assert (
        cmd.run(_args(tmp_path, title=None, specialization=["Врач-хирург"], dry_run=True)) is True
    )

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "Врач-хирург" in out
    assert "Врач" in out
    assert "[INFO] Ничего не записано" not in out
    # Панель закрыта уходом со страницы; кликов по форме в dry-run нет.
    assert env.clicks == []


def test_editor_dry_run_skips_tree_validation_for_wizard_flow(env, capsys, tmp_path, monkeypatch):
    """Wizard-поток не открывает editor-форму — его валидатором является
    live-каталог поиска вакансий (resolve_explicit_role), не дерево резюме."""
    flow = SimpleNamespace(
        kind="wizard",
        resume_id=RESUME_ID,
        values=PositionValues(title="QA"),
        state=SimpleNamespace(next_incomplete_screen_id="professional_role"),
    )
    monkeypatch.setattr(
        hhru_bot.resume_position,
        "open_position_form",
        lambda page, resume, enter_wizard=True: flow,
    )
    monkeypatch.setattr("hhru_bot.resume_titles.account_duplicate_reason", lambda *a, **kw: "")
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda page, label: SimpleNamespace(role_id="148", label=label, category="Медицина"),
    )

    assert cmd.run(_args(tmp_path, title=None, specialization=["Инженер"], dry_run=True)) is False

    assert env.validated == []
    assert "[INFO] Ничего не записано на hh.ru." in capsys.readouterr().out

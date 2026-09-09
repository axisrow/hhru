"""Characterization-тесты оркестрации боевого импорта (#1049, участок 6 аудита #1035).

Браузерные вызовы подменяются фейками на уровне МОДУЛЕЙ-источников
(``hhru_bot.create_resume``, ``hhru_bot.resume_photo``, ...): characterization
пишется до рефакторинга и обязана пережить вынос ``_run_live`` в сервисный
модуль без правок. Проверяется последовательность durable-учёта и отчёта:
прерывание после создания, ошибка отдельной секции, uncertain фото,
нечитаемая/полная галерея, сбой финальной сверки.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hhru_bot.export_resume import EXPORT_SCHEMA
from hhru_bot.history import History
from hhru_bot.import_resume import (
    plan_education,
    plan_experience,
    plan_languages,
    plan_position,
    plan_skills,
)

pytestmark = pytest.mark.unit

NEW_ID = "00002"

PAYLOAD = {
    "schema": EXPORT_SCHEMA,
    "resume_id": "00001",
    "slug": "main",
    "resume_url": "https://hh.ru/resume/00001",
    "position": {"title": "Python-разработчик", "salary_text": None, "fields": []},
    "about": "Разрабатываю сервисы.",
    "experience": {
        "companies": [
            {
                "company": "ООО Тест",
                "positions": [
                    {
                        "title": "Разработчик",
                        "period": "Март 2020 — Март 2024",
                        "description": "Писал код",
                    }
                ],
            }
        ]
    },
    "education": [{"id": "1", "title": "МГТУ", "subtitle": "Факультет ИУ", "description": "2020"}],
    "skills": [{"id": "1", "name": "Python"}],
    "languages": [{"id": "1", "title": "Английский", "subtitle": "B2"}],
    "photos": [],
}


class _FakePage:
    def close(self) -> None:
        pass


class _FakeContext:
    def new_page(self) -> _FakePage:
        return _FakePage()


@contextmanager
def _fake_launch(_state, headless=True, user_agent=None):  # noqa: ANN001, ANN002
    yield _FakeContext()


def _ok_create_result():
    return SimpleNamespace(
        success=True,
        uncertain=False,
        reason="черновик создан",
        new_resume_id=NEW_ID,
        placeholder_role=False,
    )


def _run_import(monkeypatch, tmp_path: Path, payload: dict, *, impls: dict | None = None):
    """Прогнать _run_live-оркестрацию с фейковыми браузерными модулями."""
    from types import SimpleNamespace as NS

    history = History(tmp_path / "history.db")
    position_plan, _ = plan_position(payload)
    experience_plan, _ = plan_experience(payload)
    education_plan, _ = plan_education(payload)
    skills, _ = plan_skills(payload)
    languages, _ = plan_languages(payload)

    calls: dict[str, list] = {"uploads": [], "sections": []}
    default_impls = {
        "hhru_bot.browser.launch_context": _fake_launch,
        "hhru_bot.catalog_preflight.preflight_profession": lambda page, title, allow_unresolved_area: (
            NS(ok=True, message="")
        ),
        "hhru_bot.create_resume.create_resume_on_hh": lambda *a, **k: _ok_create_result(),
        "hhru_bot.create_resume.apply_draft_readback": lambda page, result: NS(
            reason="readback ok"
        ),
        "hhru_bot.resume_position.open_position_form": lambda *a, **k: NS(
            kind="editor", values=NS()
        ),
        "hhru_bot.resume_position.apply_position": lambda *a, **k: calls["sections"].append(
            "position"
        ),
        "hhru_bot.resume_position.click_save_and_wait": lambda page: None,
        "hhru_bot.commands.resume_position._click_save_and_wait": lambda page: None,
        "hhru_bot.about.open_about_editor": lambda page, resume: "",
        "hhru_bot.about.save_about": lambda page, text: calls["sections"].append("about"),
        "hhru_bot.copy_resume.list_resume_cards": lambda *a, **k: [],
        "hhru_bot.experience.edit_experience_on_hh": lambda *a, **k: [
            NS(success=True, uncertain=False, reason=None)
        ],
        "hhru_bot.resume_education.edit_education_on_hh": lambda *a, **k: [
            NS(success=True, uncertain=False, reason=None)
        ],
        "hhru_bot.skills.edit_skills_on_hh": lambda *a, **k: NS(
            success=True, reason=None, added=None
        ),
        "hhru_bot.languages.edit_languages_on_hh": lambda *a, **k: NS(
            success=True, reason=None, acted=False
        ),
        "hhru_bot.resume_photo.select_photo_on_hh": lambda *a, **k: NS(
            success=True, photos=(), reason="план пуст"
        ),
        "hhru_bot.resume_photo.validate_photo": lambda path: path,
        "hhru_bot.resume_photo.upload_photo_on_hh": lambda *a, **k: (
            calls["uploads"].append(a[-1] if a else k)
            or NS(success=True, uncertain=False, reason=None)
        ),
        "hhru_bot.export_resume.export_resume_on_hh": lambda *a, **k: NS(
            success=True, payload=payload, reason=None
        ),
    }
    default_impls.update(impls or {})
    for target, impl in default_impls.items():
        monkeypatch.setattr(target, impl, raising=False)

    args = argparse.Namespace(
        headless=True, output=tmp_path / "imports", command="import-resume", no_photos=False
    )
    config = NS(storage_state_file="", user_agent=None)
    from hhru_bot.commands.import_resume import _run_live

    result = _run_live(
        args,
        history,
        config,
        payload,
        position_plan,
        experience_plan,
        education_plan,
        skills,
        languages,
        [tmp_path / f"photo_{i}.jpeg" for i in range(1, 4)],
        [],
    )
    return result, history, calls


def test_happy_path_reports_success_and_clean_diff(capsys, tmp_path, monkeypatch):
    result, history, calls = _run_import(monkeypatch, tmp_path, PAYLOAD)
    out = capsys.readouterr().out
    assert result is False
    assert calls["sections"] == ["position", "about"]
    assert "[OK] создание:" in out
    assert "[OK] позиция: сохранена в editor-режиме" in out
    assert "[OK] о себе: сохранено" in out
    assert "[OK] сверка:" in out


def test_section_exception_after_create_propagates(capsys, tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("форма позиции не открылась")

    with pytest.raises(RuntimeError, match="форма позиции"):
        _run_import(
            monkeypatch,
            tmp_path,
            PAYLOAD,
            impls={
                "hhru_bot.resume_position.apply_position": _boom,
            },
        )
    # Characterization: секции без before_click не имеют durable-маркера —
    # прерывание секции не оставляет uncertain-строки edit_position (в отличие
    # от create/upload-photo), а исключение доходит до supervisor'а (run failed).
    history = History(tmp_path / "history.db")
    assert not history.has_unresolved_uncertain(NEW_ID, "edit_position")
    out = capsys.readouterr().out
    assert "[OK] создание:" in out
    assert "status=failed" in out


def test_about_generation_error_fails_section_and_continues(capsys, tmp_path, monkeypatch):
    from hhru_bot.about import AboutGenerationError

    def _fail(*a, **k):
        raise AboutGenerationError("LLM недоступен")

    result, history, calls = _run_import(
        monkeypatch,
        tmp_path,
        PAYLOAD,
        impls={
            "hhru_bot.about.save_about": _fail,
        },
    )
    out = capsys.readouterr().out
    assert result is True
    assert "[FAIL] о себе: LLM недоступен" in out
    # прочие секции и сверка продолжились
    assert calls["sections"] == ["position"]
    assert "[OK] позиция" in out
    assert "[OK] сверка" in out


def test_uncertain_photo_with_overflow_reports_both(capsys, tmp_path, monkeypatch):
    def _inventory(*a, **k):
        return SimpleNamespace(success=True, photos=(*[object()] * 7,), reason="план")

    uploaded: list = []

    def _upload(page, resume, photo_file, dry_run, before_click=None):  # noqa: ANN001
        if before_click is not None:
            before_click()  # реальный upload_photo_on_hh резервирует мутацию здесь
        uploaded.append(photo_file)
        return SimpleNamespace(success=False, uncertain=True, reason="серый исход")

    result, history, calls = _run_import(
        monkeypatch,
        tmp_path,
        PAYLOAD,
        impls={
            "hhru_bot.resume_photo.select_photo_on_hh": _inventory,
            "hhru_bot.resume_photo.upload_photo_on_hh": _upload,
        },
    )
    out = capsys.readouterr().out
    assert result is True
    assert "[WARN] фото: свободных слотов 1, файлов 3" in out
    assert "[WARN] (uncertain) фото photo_1.jpeg: серый исход" in out
    assert len(uploaded) == 1  # только влезающий слот
    assert history.has_unresolved_uncertain(NEW_ID, "upload_photo")


def test_full_gallery_refuses_without_uploads(capsys, tmp_path, monkeypatch):
    def _inventory(*a, **k):
        return SimpleNamespace(success=True, photos=(*[object()] * 8,), reason="план")

    result, history, calls = _run_import(
        monkeypatch,
        tmp_path,
        PAYLOAD,
        impls={
            "hhru_bot.resume_photo.select_photo_on_hh": _inventory,
        },
    )
    out = capsys.readouterr().out
    assert result is True
    assert "[FAIL] фото: галерея аккаунта полна (8/8)" in out
    assert calls["uploads"] == []


def test_unreadable_gallery_cancels_uploads(capsys, tmp_path, monkeypatch):
    result, history, calls = _run_import(
        monkeypatch,
        tmp_path,
        PAYLOAD,
        impls={
            "hhru_bot.resume_photo.select_photo_on_hh": lambda *a, **k: SimpleNamespace(
                success=False, photos=(), reason="анти-бот"
            ),
        },
    )
    out = capsys.readouterr().out
    assert result is True
    assert "[FAIL] фото: инвентарь галереи не прочитан (анти-бот)" in out
    assert calls["uploads"] == []


def test_final_verification_failure_is_reported(capsys, tmp_path, monkeypatch):
    result, history, calls = _run_import(
        monkeypatch,
        tmp_path,
        PAYLOAD,
        impls={
            "hhru_bot.export_resume.export_resume_on_hh": lambda *a, **k: SimpleNamespace(
                success=False, payload=None, reason="нет доступа"
            ),
        },
    )
    out = capsys.readouterr().out
    assert result is True
    assert "[FAIL] сверка: контрольное чтение не удалось (нет доступа)" in out


def test_verification_discrepancy_lists_diff_lines(capsys, tmp_path, monkeypatch):
    imported = json.loads(json.dumps(PAYLOAD))
    imported["position"]["title"] = "Другая роль"
    result, _, _ = _run_import(
        monkeypatch,
        tmp_path,
        PAYLOAD,
        impls={
            "hhru_bot.export_resume.export_resume_on_hh": lambda *a, **k: SimpleNamespace(
                success=True, payload=imported, reason=None
            ),
        },
    )
    out = capsys.readouterr().out
    assert result is False  # расхождение — не сбой команды, только [WARN]
    assert "расхождение: " in out

"""Готовый текст в write-команды заполнения резюме без LLM (#326).

Контракт: при передаче готового значения (как edit-skills --skill) команда
не требует ни ai_profile у резюме, ни секции ai; LLM не вызывается;
невалидный ручной ввод фейлится до любых действий; конфликт ручного флага
с LLM-аргументами (--career/--source/--mode) — явная ошибка.
"""

from __future__ import annotations

import argparse
import textwrap
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hhru_bot.commands import (
    about as about_cmd,
)
from hhru_bot.commands import (
    edit_education as edit_education_cmd,
)
from hhru_bot.commands import (
    edit_experience as edit_experience_cmd,
)
from hhru_bot.commands import (
    resume_position as resume_position_cmd,
)
from hhru_bot.commands import (
    resume_sections as resume_sections_cmd,
)

pytestmark = pytest.mark.unit

REMOTE_ONLY_HASH = "35661ef3ff10f971a70039ed1f57656d684c54ab"


def _write_config(tmp_path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            """
            account:
              storage_state_file: data/storage_state/hh_session.json
            """
        ),
        encoding="utf-8",
    )
    return str(path)


def _args(tmp_path, **overrides) -> argparse.Namespace:
    base = {
        "config": _write_config(tmp_path),
        "history": str(tmp_path / "h.db"),
        "headless": True,
        "resume": REMOTE_ONLY_HASH,
        "dry_run": True,
        "force": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class _FakeLocator:
    def count(self) -> int:
        return 1

    def click(self) -> None:
        return None

    def wait_for(self, *, timeout=None, state=None) -> None:
        return None


class _FakePage:
    def locator(self, _selector) -> _FakeLocator:
        return _FakeLocator()

    def new_page(self) -> _FakePage:
        return self


@contextmanager
def _fake_launch_context(*_args, **_kwargs):
    yield _FakePage()


# --- edit-experience --entry -------------------------------------------------


def test_edit_experience_manual_entry_dry_run_without_ai(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    edit_experience_cmd.run(
        _args(
            tmp_path,
            mode="fill",
            career=None,
            existing=None,
            entry=['{"company": "ООО Тест", "position": "Инженер", "duties": "Делал дело"}'],
        )
    )
    out = capsys.readouterr().out
    assert "ООО Тест" in out
    assert "[DRY-RUN] save не нажат" in out


def test_edit_experience_manual_entry_invalid_json_fails_closed(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        edit_experience_cmd.run(
            _args(tmp_path, mode="fill", career=None, existing=None, entry=["{"])
        )
    assert exc.value.code == 1
    assert "валидный JSON" in capsys.readouterr().out


def test_edit_experience_manual_entry_requires_company_and_position(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        edit_experience_cmd.run(
            _args(tmp_path, mode="fill", career=None, existing=None, entry=['{"duties": "x"}'])
        )
    assert exc.value.code == 1
    assert "company и position" in capsys.readouterr().out


def test_edit_experience_entry_conflicts_with_career(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        edit_experience_cmd.run(
            _args(
                tmp_path, career="факты", existing=None, entry=['{"company": "a", "position": "b"}']
            )
        )
    assert exc.value.code == 1
    assert "LLM-планированию" in capsys.readouterr().out


def test_edit_experience_requires_career_or_entry(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        edit_experience_cmd.run(_args(tmp_path, mode="fill", career=None, existing=None))
    assert exc.value.code == 1
    assert "--career" in capsys.readouterr().out


def test_edit_experience_manual_entry_appends_after_existing_rows(tmp_path, capsys, monkeypatch):
    """Manual --entry must not overwrite an existing row by reusing index 0 (#327).

    Regression guard: a boevoy manual --entry run on a resume that already has
    experience must append a new row (index len(existing)), never reuse an
    existing index — reusing an index blanks any field the manual JSON omitted.
    """
    from hhru_bot.experience import ExperienceEntry

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.experience.read_experience_on_hh",
        lambda page, resume_id: [
            ExperienceEntry(company="Старая компания", position="Старая должность")
        ],
    )
    captured = {}

    def fake_edit_experience_on_hh(page, resume_id, plan, *, dry_run, indexes=None):
        captured["indexes"] = indexes
        captured["plan"] = plan
        return ["строка 1: сохранено"]

    monkeypatch.setattr("hhru_bot.experience.edit_experience_on_hh", fake_edit_experience_on_hh)
    edit_experience_cmd.run(
        _args(
            tmp_path,
            mode="fill",
            dry_run=False,
            force=True,
            career=None,
            existing=None,
            entry=['{"company": "Новая компания", "position": "Новая должность"}'],
        )
    )
    assert captured["indexes"] == [1]


# --- about --text ------------------------------------------------------------


def test_about_manual_text_saves_without_ai(tmp_path, capsys, monkeypatch):
    saved = {}
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr("hhru_bot.about.open_about_editor", lambda page, resume: "старый текст")
    monkeypatch.setattr(
        "hhru_bot.about.save_about", lambda page, text: saved.setdefault("text", text)
    )
    about_cmd.run(_args(tmp_path, dry_run=False, force=True, text="готовый перевод"))
    out = capsys.readouterr().out
    assert saved["text"] == "готовый перевод"
    assert "(manual)" in out
    assert "[OK]" in out


def test_about_manual_text_dry_run_does_not_save(tmp_path, capsys, monkeypatch):
    saved = {}
    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr("hhru_bot.about.open_about_editor", lambda page, resume: "старый текст")
    monkeypatch.setattr(
        "hhru_bot.about.save_about", lambda page, text: saved.setdefault("text", text)
    )
    about_cmd.run(_args(tmp_path, dry_run=True, text="черновик"))
    assert "text" not in saved
    assert "Ничего не сохранено" in capsys.readouterr().out


# --- resume-position ручные поля --------------------------------------------


def test_resume_position_manual_title_dry_run_without_ai(tmp_path, capsys, monkeypatch):
    from hhru_bot.resume_position import PositionValues

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda page, resume: PositionValues(title="старая"),
    )
    applied = {}
    monkeypatch.setattr(
        "hhru_bot.resume_position.apply_position",
        lambda page, plan: applied.setdefault("title", plan.title),
    )
    result = resume_position_cmd.run(_args(tmp_path, title="数据工程师", mode=None))
    out = capsys.readouterr().out
    assert result is False
    assert "数据工程师" in out
    assert "Ничего не записано" in out
    assert "title" not in applied


def test_resume_position_manual_conflicts_with_mode(tmp_path, capsys):
    result = resume_position_cmd.run(_args(tmp_path, title="x", mode="from-scratch"))
    assert result is True
    assert "LLM-планированию" in capsys.readouterr().out


def test_resume_position_manual_allows_explicit_fill_mode(tmp_path, capsys, monkeypatch):
    """--mode fill matches the implicit manual default and must not be rejected (#327)."""
    from hhru_bot.resume_position import PositionValues

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda page, resume: PositionValues(title="старая"),
    )
    result = resume_position_cmd.run(_args(tmp_path, title="新职位", mode="fill"))
    assert result is False
    assert "新职位" in capsys.readouterr().out


# --- edit-education ручные записи -------------------------------------------


def test_edit_education_parse_manual_records_valid():
    records = edit_education_cmd._parse_manual_records(
        "--primary-entry", ['{"institution": "МГУ", "faculty": "ВМК", "year": "2020"}']
    )
    assert records[0].institution == "МГУ"
    assert records[0].faculty == "ВМК"


def test_edit_education_parse_manual_records_invalid_json():
    with pytest.raises(ValueError, match="валидный JSON"):
        edit_education_cmd._parse_manual_records("--primary-entry", ["{"])


def test_edit_education_parse_manual_records_requires_institution():
    with pytest.raises(ValueError, match="institution"):
        edit_education_cmd._parse_manual_records("--primary-entry", ['{"faculty": "ВМК"}'])


def test_edit_education_manual_conflicts_with_source(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        edit_education_cmd.run(
            _args(tmp_path, section="both", source="факты", mode=None, institution="МГУ")
        )
    assert exc.value.code == 1
    assert "LLM-планированию" in capsys.readouterr().out


# --- resume-sections --attestation/--recommendation -------------------------


def test_resume_sections_parse_manual_plan():
    args = SimpleNamespace(
        attestation=['{"name": "IELTS", "organization": "Cambridge", "year": "2024"}'],
        recommendation=['{"text": "Рекомендует", "company": "ООО Тест"}'],
    )
    plan = resume_sections_cmd._parse_manual_sections(args)
    assert plan.attestations[0].name == "IELTS"
    assert plan.recommendations[0].company == "ООО Тест"


def test_resume_sections_parse_manual_plan_empty_record():
    with pytest.raises(ValueError, match="пустую запись"):
        resume_sections_cmd._parse_manual_sections(
            SimpleNamespace(attestation=['{"name": ""}'], recommendation=None)
        )


def test_resume_sections_parse_manual_plan_requires_any():
    with pytest.raises(ValueError, match="хотя бы один"):
        resume_sections_cmd._parse_manual_sections(
            SimpleNamespace(attestation=None, recommendation=None)
        )

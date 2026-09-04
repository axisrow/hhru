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
from unittest.mock import MagicMock

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


@pytest.fixture(autouse=True)
def _no_duplicate_titles(monkeypatch):
    """#911: дубль-гард должностей по умолчанию «нет дубля» — двойники этих
    тестов не моделируют список резюме; проводка гарда покрыта в
    test_resume_position_command.py."""
    monkeypatch.setattr(
        "hhru_bot.resume_titles.account_duplicate_reason",
        lambda page, title, exclude_resume_id="": "",
    )


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
    monkeypatch.setattr("hhru_bot.experience.read_experience_on_hh", lambda page, resume_id: [])
    result = edit_experience_cmd.run(
        _args(
            tmp_path,
            mode="fill",
            career=None,
            existing=None,
            entry=[
                '{"company": "ООО Тест", "position": "Инженер", "start_month": "3", '
                '"duties": "Делал дело"}'
            ],
        )
    )
    out = capsys.readouterr().out
    assert result is False
    assert "ООО Тест" in out
    assert "[DRY-RUN] save не нажат" in out


def test_edit_experience_manual_dry_run_prints_live_rows(tmp_path, capsys, monkeypatch):
    """#957: a MANUAL dry-run reads the live resume (read-only) and prints the
    existing row set before the [DRY-RUN] exit — that listing is the state
    check the caller needs before authorizing a battle run. The LLM dry-run
    keeps its browserless early exit (covered above)."""
    from hhru_bot.experience import ExperienceEntry

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.experience.read_experience_on_hh",
        lambda page, resume_id: [
            ExperienceEntry(
                company="Старая компания",
                position="Старая должность",
                start_year="2018",
                start_month="3",
                end_year="2023",
                end_month="12",
            )
        ],
    )
    result = edit_experience_cmd.run(
        _args(
            tmp_path,
            mode="fill",
            career=None,
            existing=None,
            entry=[
                '{"company": "Новая компания", "position": "Новая должность", "start_month": "1"}'
            ],
        )
    )
    out = capsys.readouterr().out
    assert result is False
    assert "существующий опыт на hh.ru (1)" in out
    assert "Старая компания — Старая должность (03.2018–12.2023)" in out
    assert "[DRY-RUN] save не нажат" in out


def test_edit_experience_manual_entry_invalid_json_fails_closed(tmp_path, capsys):
    # #465: run() reports a validation failure by returning `True` under the
    # durable command_run ledger instead of raising SystemExit.
    assert (
        edit_experience_cmd.run(
            _args(tmp_path, mode="fill", career=None, existing=None, entry=["{"])
        )
        is True
    )
    assert "валидный JSON" in capsys.readouterr().out


def test_edit_experience_manual_entry_requires_company_and_position(tmp_path, capsys):
    assert (
        edit_experience_cmd.run(
            _args(tmp_path, mode="fill", career=None, existing=None, entry=['{"duties": "x"}'])
        )
        is True
    )
    assert "company и position" in capsys.readouterr().out


def test_edit_experience_entry_conflicts_with_career(tmp_path, capsys):
    assert (
        edit_experience_cmd.run(
            _args(
                tmp_path, career="факты", existing=None, entry=['{"company": "a", "position": "b"}']
            )
        )
        is True
    )
    assert "LLM-планированию" in capsys.readouterr().out


def test_edit_experience_entry_rejects_llm_only_mode(tmp_path, capsys):
    """#921: --mode=create is LLM-planning-only; with --entry it must fail
    with an actionable hint (remove --mode / pass the fill default), not a
    contract that is only discoverable from the failure text itself."""
    assert (
        edit_experience_cmd.run(
            _args(
                tmp_path,
                mode="create",
                career=None,
                existing=None,
                entry=['{"company": "a", "position": "b", "start_month": "3"}'],
            )
        )
        is True
    )
    out = capsys.readouterr().out
    assert "LLM-планированию" in out
    assert "--help" in out


def test_edit_experience_requires_career_or_entry(tmp_path, capsys):
    assert edit_experience_cmd.run(_args(tmp_path, mode="fill", career=None, existing=None)) is True
    assert "--career" in capsys.readouterr().out


def test_edit_experience_manual_entry_rejects_duplicate_row(tmp_path, capsys, monkeypatch):
    """#957: --entry appends through the confirmed add shape instead of
    refusing on a non-empty resume — but a manual entry that duplicates an
    existing row's identity (company+position+start) must be rejected: a
    re-run after an uncertain outcome must not add a second copy of a row
    the earlier attempt already saved. No edit_experience_on_hh call may
    happen (and no attempt counted)."""
    from hhru_bot.experience import ExperienceEntry

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.experience.read_experience_on_hh",
        lambda page, resume_id: [
            ExperienceEntry(
                company="ТСЖ Дом",
                position="Дворник",
                start_year="2024",
                start_month="1",
                current=True,
            )
        ],
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("edit_experience_on_hh must not be called for a duplicate row")

    monkeypatch.setattr("hhru_bot.experience.edit_experience_on_hh", fail_if_called)
    assert (
        edit_experience_cmd.run(
            _args(
                tmp_path,
                mode="fill",
                dry_run=False,
                force=True,
                career=None,
                existing=None,
                entry=[
                    '{"company": "ТСЖ Дом", "position": "Дворник", '
                    '"start_year": "2024", "start_month": "1", "current": true}'
                ],
            )
        )
        is True
    )
    out = capsys.readouterr().out
    assert "существующий опыт на hh.ru (1)" in out
    assert "[FAIL] запись уже существует" in out
    assert "добавление дубля отклонено" in out


def test_edit_experience_manual_entry_appends_to_non_empty_resume(tmp_path, capsys, monkeypatch):
    """#957: a NON-duplicate manual entry on a resume that already has rows is
    appended via edit_experience_on_hh with append_only=True — the shared
    add shape is forced, so a colliding row index can never route the plan
    onto an existing row's editor (the #815 overwrite trap the old outright
    refusal guarded against)."""
    from hhru_bot.experience import ExperienceEntry, ExperienceResult

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.experience.read_experience_on_hh",
        lambda page, resume_id: [
            ExperienceEntry(company="Старая компания", position="Старая должность")
        ],
    )
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", lambda page: [])
    captured = {}

    def fake_edit_experience_on_hh(
        page, resume_id, plan, *, dry_run, indexes=None, resume_titles=None, append_only=False
    ):
        captured["indexes"] = indexes
        captured["append_only"] = append_only
        return [ExperienceResult("строка 0: сохранено", success=True)]

    monkeypatch.setattr("hhru_bot.experience.edit_experience_on_hh", fake_edit_experience_on_hh)
    assert (
        edit_experience_cmd.run(
            _args(
                tmp_path,
                mode="fill",
                dry_run=False,
                force=True,
                career=None,
                existing=None,
                entry=[
                    '{"company": "Новая компания", "position": "Новая должность", "start_month": "5"}'
                ],
            )
        )
        is False
    )
    out = capsys.readouterr().out
    assert "существующий опыт на hh.ru (1)" in out
    assert "Старая компания — Старая должность" in out
    assert captured["indexes"] == [0]
    assert captured["append_only"] is True


def test_edit_experience_manual_entry_creates_first_row_on_empty_resume(
    tmp_path, capsys, monkeypatch
):
    """A resume with zero existing rows must still create its first row via
    --entry through the #786/#787-confirmed route (append_only only forces
    the add shape when there IS something to append after)."""
    from hhru_bot.experience import ExperienceResult

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr("hhru_bot.experience.read_experience_on_hh", lambda page, resume_id: [])
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", lambda page: [])
    captured = {}

    def fake_edit_experience_on_hh(
        page, resume_id, plan, *, dry_run, indexes=None, resume_titles=None, append_only=False
    ):
        captured["indexes"] = indexes
        captured["append_only"] = append_only
        return [ExperienceResult("строка 0: сохранено", success=True)]

    monkeypatch.setattr("hhru_bot.experience.edit_experience_on_hh", fake_edit_experience_on_hh)
    edit_experience_cmd.run(
        _args(
            tmp_path,
            mode="fill",
            dry_run=False,
            force=True,
            career=None,
            existing=None,
            entry=[
                '{"company": "Новая компания", "position": "Новая должность", "start_month": "5"}'
            ],
        )
    )
    out = capsys.readouterr().out
    assert "существующий опыт на hh.ru: записей нет" in out
    assert captured["indexes"] == [0]
    assert captured["append_only"] is True


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
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda page, resume: PositionFlowContext(
            "editor",
            resume.resume_id,
            PositionValues(title="старая"),
            ResumeState(status="new", is_searchable=True),
        ),
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
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    monkeypatch.setattr("hhru_bot.browser.launch_context", _fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda page, resume: PositionFlowContext(
            "editor",
            resume.resume_id,
            PositionValues(title="старая"),
            ResumeState(status="new", is_searchable=True),
        ),
    )
    result = resume_position_cmd.run(_args(tmp_path, title="新职位", mode="fill"))
    assert result is False
    assert "新职位" in capsys.readouterr().out


def test_resume_position_draft_dry_run_resolves_explicit_live_role(tmp_path, capsys, monkeypatch):
    from hhru_bot.professional_roles import ProfessionalRole
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    class _WizardPage(_FakePage):
        url = "https://hh.ru/profile/resume/professional_role?resume=" + REMOTE_ONLY_HASH

    @contextmanager
    def _wizard_context(*_args, **_kwargs):
        yield _WizardPage()

    monkeypatch.setattr("hhru_bot.browser.launch_context", _wizard_context)
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda page, resume, **_kwargs: PositionFlowContext(
            "wizard",
            resume.resume_id,
            PositionValues(title="AI Team Lead"),
            ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
        ),
    )
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda page, label: ProfessionalRole("104", label, "Информационные технологии"),
    )
    save = MagicMock()
    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard", save)

    result = resume_position_cmd.run(
        _args(
            tmp_path,
            title="AI Team Lead",
            specialization=["Руководитель группы разработки"],
            mode=None,
        )
    )

    out = capsys.readouterr().out
    assert result is False
    assert "[CLASSIFICATION]" in out
    assert "role_id: 104" in out
    assert "Ничего не записано" in out
    save.assert_not_called()


def test_resume_position_wizard_write_rebinds_and_never_reports_editor_success(
    tmp_path, capsys, monkeypatch
):
    from hhru_bot.professional_roles import ProfessionalRole
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    class _WizardPage(_FakePage):
        url = "https://hh.ru/profile/resume/professional_role?resume=" + REMOTE_ONLY_HASH

    @contextmanager
    def _wizard_context(*_args, **_kwargs):
        yield _WizardPage()

    flow = PositionFlowContext(
        "wizard",
        REMOTE_ONLY_HASH,
        PositionValues(title="AI Engineer"),
        ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
    )
    editor_flow = PositionFlowContext(
        "editor", REMOTE_ONLY_HASH, PositionValues(title="Администратор"), ResumeState(status="new")
    )
    open_flow = MagicMock(side_effect=[flow, flow, editor_flow])
    save = MagicMock()
    monkeypatch.setattr("hhru_bot.browser.launch_context", _wizard_context)
    monkeypatch.setattr("hhru_bot.resume_position.open_position_form", open_flow)
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda page, label: ProfessionalRole("10", label, "Информационные технологии"),
    )
    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard_minimum", save)
    monkeypatch.setattr(
        "hhru_bot.resume_position.verify_wizard_minimum_save",
        lambda *_args, **_kwargs: ResumeState(status="new", is_searchable=True),
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.verify_wizard_save",
        lambda *_args, **_kwargs: ResumeState(status="new", is_searchable=False),
    )
    monkeypatch.setattr("hhru_bot.resume_position.apply_position", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "hhru_bot.commands.resume_position._click_save_and_wait", lambda *_args: None
    )

    result = resume_position_cmd.run(
        _args(
            tmp_path,
            title="AI Engineer",
            specialization=["Аналитик"],
            mode=None,
            dry_run=False,
            force=True,
            allow_auto_publish=True,
        )
    )

    out = capsys.readouterr().out
    assert result is False
    assert open_flow.call_count == 3
    save.assert_called_once()
    assert "[OK] professional_role" in out
    assert "точная специализация применена" in out
    assert "[OK] Раздел желаемой работы" not in out


def test_resume_position_write_success_does_not_double_count_on_exit_error(
    tmp_path, capsys, monkeypatch
):
    """A successful write must not also be counted as failed (#465 review).

    Regression guard for the cycle-review PR #472 finding: applied_count was
    incremented, then `return False` unwinds the `with launch_context(...)`
    block — if `context.__exit__` itself raises during that unwind, the outer
    `except Exception` handler must not ALSO add failed_count for the same
    attempt (attempted=1 success=1 failed=1 would be a lie: the write did
    land, per the applied_count already recorded).
    """
    from hhru_bot.history import History
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    @contextmanager
    def _exploding_launch_context(*_args, **_kwargs):
        yield _FakePage()
        raise RuntimeError("browser close failed")

    monkeypatch.setattr("hhru_bot.browser.launch_context", _exploding_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda page, resume: PositionFlowContext(
            "editor",
            resume.resume_id,
            PositionValues(title="старая"),
            ResumeState(status="new", is_searchable=True),
        ),
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.apply_position",
        lambda *_args, **_kwargs: None,
    )

    history_path = tmp_path / "h.db"
    result = resume_position_cmd.run(
        _args(
            tmp_path,
            title="新职位",
            mode=None,
            dry_run=False,
            force=True,
            history=str(history_path),
        )
    )
    assert result is True  # the command still reports failure (exit unwind raised)

    row = History(history_path).command_runs()[-1]
    assert row["attempted"] == 1
    assert row["success"] == 1
    # The write itself landed (success=1); the __exit__ failure must not also
    # be double-counted as a failure for the same single attempt.
    assert row["failed"] == 0


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
    assert (
        edit_education_cmd.run(
            _args(tmp_path, section="both", source="факты", mode=None, institution="МГУ")
        )
        is True
    )
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

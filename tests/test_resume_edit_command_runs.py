"""Durable-ledger wiring for the single-mutation resume edit commands (#465)."""

from __future__ import annotations

import argparse
import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hhru_bot.history import History

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("module_name", "command_name"),
    [
        ("edit_experience", "edit_experience"),
        ("edit_education", "edit_education"),
        ("edit_skills", "edit_skills"),
        ("edit_languages", "edit_languages"),
        ("resume_position", "resume_position"),
    ],
)
def test_successful_resume_edit_persists_one_complete_command_run(
    tmp_path: Path, monkeypatch, capsys, module_name: str, command_name: str
) -> None:
    command = importlib.import_module(f"hhru_bot.commands.{module_name}")

    def mutation(_args, progress):
        progress.begin_attempt()
        progress.applied_count += 1
        return False

    monkeypatch.setattr(command, "_run", mutation)
    history_path = tmp_path / "history.db"

    assert command.run(argparse.Namespace(history=str(history_path))) is False

    row = History(history_path).command_runs()[-1]
    assert row["command"] == command_name
    assert row["status"] == "completed"
    assert row["attempted"] == row["success"] == 1
    assert row["failed"] == row["uncertain"] == row["skipped"] == 0
    assert "attempted=1 success=1 failed=0 uncertain=0 skipped=0" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("module_name", "command_name"),
    [
        ("edit_experience", "edit_experience"),
        ("edit_education", "edit_education"),
        ("edit_skills", "edit_skills"),
        ("edit_languages", "edit_languages"),
        ("resume_position", "resume_position"),
    ],
)
def test_failed_mutation_after_attempt_persists_partial_status(
    tmp_path: Path, monkeypatch, module_name: str, command_name: str
) -> None:
    """A failure AFTER begin_attempt() must record status='partial' (#465 review).

    Regression guard for the edit_languages.py bug found in cycle-review of
    PR #472: a body that still called sys.exit()/raised SystemExit past the
    attempt-reservation point escaped run_supervised_command's normal
    bool-based classification (the generic ``except BaseException`` branch
    never computes final_status), recording status='failed' instead of the
    'partial' every other command produces for the identical one-attempt-
    failed outcome.
    """
    command = importlib.import_module(f"hhru_bot.commands.{module_name}")

    def mutation(_args, progress):
        progress.begin_attempt()
        progress.failed_count += 1
        return True

    monkeypatch.setattr(command, "_run", mutation)
    history_path = tmp_path / "history.db"

    assert command.run(argparse.Namespace(history=str(history_path))) is True

    row = History(history_path).command_runs()[-1]
    assert row["command"] == command_name
    assert row["status"] == "partial"
    assert row["attempted"] == row["failed"] == 1
    assert row["success"] == row["uncertain"] == row["skipped"] == 0


def test_edit_languages_manual_write_failure_records_partial_not_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for the dead-code/status bug found in cycle-review of
    PR #472: a failed manual ``--language`` write must return True (not raise
    SystemExit via a leftover ``_report()`` call) and the ledger must show
    ``status='partial'`` like every sibling command, not ``'failed'``.
    """
    import hhru_bot.commands.edit_languages as command
    from hhru_bot.languages import Language, LanguagesResult

    resume = SimpleNamespace(id="r1", resume_id="r1")
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.languages.edit_languages_on_hh",
        lambda *_a, **_kw: LanguagesResult(
            success=False, proposed=(Language("English", "B1"),), reason="запись не подтверждена"
        ),
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        mode="append",
        language=["English=B1"],
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "запись не подтверждена" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_languages"
    assert row["status"] == "partial"
    assert row["attempted"] == row["failed"] == 1
    assert row["success"] == row["uncertain"] == row["skipped"] == 0


def test_resume_position_without_ai_profile_explains_manual_catalog_workflow(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import hhru_bot.commands.resume_position as command

    resume = SimpleNamespace(
        id="ai-engineer",
        resume_id="r1",
        ai_profile=None,
        search=SimpleNamespace(text="AI engineer LLM агент"),
    )
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)
    monkeypatch.setattr(
        "hhru_bot.browser.launch_context",
        lambda *_a, **_kw: pytest.fail("ошибка должна возникнуть до браузера"),
    )
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="ai-engineer",
        title=None,
        specialization=None,
        salary=None,
        currency=None,
        employment=None,
        work_format=None,
        commute=None,
        business_trips=None,
        mode=None,
        dry_run=True,
        force=False,
        history=str(tmp_path / "history.db"),
    )

    assert command.run(args) is True

    out = capsys.readouterr().out
    assert "не настроен ai_profile" in out
    assert "resume.search.text='AI engineer LLM агент'" in out
    assert "professional-roles --query" in out
    assert "resume-position --resume ai-engineer" in out


def test_edit_education_uncertain_outcome_is_not_counted_as_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An 'uncertain' mutation outcome must land in progress.uncertain_count,
    not failed_count (#465 review): CLAUDE.md/#176 treat 'uncertain' as
    'may have landed', which a ledger reading failed=1 uncertain=0 hides from
    the operator exactly the way this project fails closed against.

    Drives the real edit_education._run body (not a stub of _run itself) by
    mocking only edit_education_on_hh, per the code-reviewer agent's finding
    that a monkeypatched _run never exercises the fix.
    """
    import hhru_bot.commands.edit_education as command
    from hhru_bot.resume_education import EducationResult

    resume = SimpleNamespace(
        id="r1", resume_id="r1", resume_url="https://hh.ru/resume/r1", education=None
    )
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_education.edit_education_on_hh",
        lambda *_a, **_kw: [
            EducationResult(kind="primary", success=False, reason="не подтверждено", uncertain=True)
        ],
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        section="both",
        source=None,
        mode=None,
        institution="МГУ",
        faculty=None,
        specialty=None,
        year=None,
        primary_entry=None,
        additional_entry=None,
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "(uncertain)" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_education"
    assert row["attempted"] == row["uncertain"] == 1
    assert row["failed"] == row["success"] == row["skipped"] == 0


def test_edit_experience_uncertain_outcome_is_not_counted_as_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Same regression as above, for edit_experience._run (#465 review)."""
    import hhru_bot.commands.edit_experience as command
    from hhru_bot.experience import ExperienceResult

    resume = SimpleNamespace(id="r1", resume_id="r1")
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr("hhru_bot.experience.read_experience_on_hh", lambda *_a, **_kw: [])
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "hhru_bot.experience.edit_experience_on_hh",
        lambda *_a, **_kw: [ExperienceResult("строка 1: не подтверждено", uncertain=True)],
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        mode="fill",
        career=None,
        existing=None,
        entry=['{"company": "a", "position": "b", "start_month": "1"}'],
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "(uncertain)" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_experience"
    assert row["attempted"] == row["uncertain"] == 1
    assert row["failed"] == row["success"] == row["skipped"] == 0


def test_edit_languages_launch_failure_before_attempt_does_not_count_as_attempted(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for BLOCKING A found in cycle-review round 2 of PR #472:
    the manual-write block had no try/except at all, so a launch_context
    failure escaped run_supervised_command's bool-based classification
    entirely (raw traceback, attempted=1 with every outcome column at 0).
    A launch failure before begin_attempt() must not be counted as attempted.
    """
    import hhru_bot.commands.edit_languages as command

    resume = SimpleNamespace(id="r1", resume_id="r1")
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    def exploding_launch_context(*_args, **_kwargs):
        raise RuntimeError("browser launch failed")

    monkeypatch.setattr("hhru_bot.browser.launch_context", exploding_launch_context)

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        mode="append",
        language=["English=B1"],
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "browser launch failed" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_languages"
    assert row["status"] == "failed"
    assert row["attempted"] == 0
    assert row["failed"] == row["success"] == row["uncertain"] == row["skipped"] == 0


def test_edit_skills_mutation_exception_after_attempt_is_recorded_as_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for a second BLOCKING finding in cycle-review round 2
    of PR #472: edit_skills_on_hh() was called with no try/except after
    begin_attempt(), so any exception it raised escaped run_supervised_command's
    bool-based classification (attempted=1 with every outcome column at 0,
    plus a raw traceback to the user).
    """
    import hhru_bot.commands.edit_skills as command

    resume = SimpleNamespace(id="r1", resume_id="r1")
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    def exploding_edit_skills_on_hh(*_a, **_kw):
        raise RuntimeError("skills form drifted")

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr("hhru_bot.skills.edit_skills_on_hh", exploding_edit_skills_on_hh)

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        mode="append",
        skill=["Python=advanced"],
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "skills form drifted" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_skills"
    assert row["status"] == "partial"
    assert row["attempted"] == row["failed"] == 1
    assert row["success"] == row["uncertain"] == row["skipped"] == 0


def test_edit_education_hard_failure_wins_over_uncertain_in_same_batch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for a third finding in cycle-review round 2 of PR #472:
    with --section both, a definite hard failure on one block was reported as
    merely 'uncertain' whenever any OTHER block in the same batch happened to
    be uncertain, because the old check was "any uncertain in the whole
    batch" rather than "is THIS failure uncertain".
    """
    import hhru_bot.commands.edit_education as command
    from hhru_bot.resume_education import EducationResult

    resume = SimpleNamespace(
        id="r1", resume_id="r1", resume_url="https://hh.ru/resume/r1", education=None
    )
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_education.edit_education_on_hh",
        lambda *_a, **_kw: [
            EducationResult(
                kind="primary",
                success=False,
                reason="строка образования отсутствует",
                uncertain=False,
            ),
            EducationResult(
                kind="additional",
                success=False,
                reason="сохранение не подтверждено",
                uncertain=True,
            ),
        ],
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        section="both",
        source=None,
        mode=None,
        institution="МГУ",
        faculty=None,
        specialty=None,
        year=None,
        primary_entry=None,
        additional_entry=None,
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_education"
    # The definite hard failure (primary) must dominate the ledger status,
    # not the co-occurring uncertain result (additional).
    assert row["attempted"] == row["failed"] == 1
    assert row["uncertain"] == row["success"] == row["skipped"] == 0


def test_edit_education_mutation_exception_after_attempt_is_recorded_as_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for a fourth finding in cycle-review round 2 of PR
    #472: only `except NotAuthenticated` guarded edit_education_on_hh() after
    begin_attempt(), so any OTHER exception (e.g. a selector-drift
    TimeoutError) escaped uncaught instead of being recorded as failed.
    """
    import hhru_bot.commands.edit_education as command

    resume = SimpleNamespace(
        id="r1", resume_id="r1", resume_url="https://hh.ru/resume/r1", education=None
    )
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    def exploding_edit_education_on_hh(*_a, **_kw):
        raise RuntimeError("education form drifted")

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_education.edit_education_on_hh", exploding_edit_education_on_hh
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        section="both",
        source=None,
        mode=None,
        institution="МГУ",
        faculty=None,
        specialty=None,
        year=None,
        primary_entry=None,
        additional_entry=None,
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "education form drifted" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_education"
    assert row["status"] == "partial"
    assert row["attempted"] == row["failed"] == 1
    assert row["success"] == row["uncertain"] == row["skipped"] == 0


def test_resume_position_grey_zone_post_click_failure_is_uncertain_not_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for a BLOCKING finding in cycle-review round 3 of PR
    #472: the CLAUDE.md #207 grey-zone post-click failure (save click landed,
    confirmation timed out) was recorded as failed_count instead of
    uncertain_count, losing the "may have landed" fail-closed semantic.
    """
    import hhru_bot.commands.resume_position as command
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    resume = SimpleNamespace(id="r1", resume_id="r1", ai_profile=object())
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=object())
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    class _FailingLocator:
        def count(self):
            return 1

        def click(self):
            return None

        def wait_for(self, *, state=None, timeout=None):
            raise TimeoutError("form did not hide")

    class _Page:
        def locator(self, _selector):
            return _FailingLocator()

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: _Page())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
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
        "hhru_bot.resume_position.apply_position", lambda page, plan, current=None: None
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        title="новая",
        specialization=None,
        salary=None,
        currency=None,
        employment=None,
        work_format=None,
        commute=None,
        business_trips=None,
        mode=None,
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "(uncertain)" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "resume_position"
    assert row["attempted"] == row["uncertain"] == 1
    assert row["failed"] == row["success"] == row["skipped"] == 0


@pytest.mark.parametrize(
    ("click_started", "expected_status"), [(False, "failed"), (True, "uncertain")]
)
def test_draft_position_classifies_failure_at_first_click_boundary(
    tmp_path: Path, monkeypatch, capsys, click_started: bool, expected_status: str
) -> None:
    import hhru_bot.commands.resume_position as command
    from hhru_bot.professional_roles import ProfessionalRole
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    resume = SimpleNamespace(id="r1", resume_id="r1", ai_profile=None)
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    page = SimpleNamespace(url="https://hh.ru/profile/resume/professional_role?resume=r1")

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: page)

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda _page, _resume: PositionFlowContext(
            "wizard",
            _resume.resume_id,
            PositionValues(title="AI Team Lead"),
            ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
        ),
    )
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda _page, label: ProfessionalRole("104", label, "ИТ"),
    )

    def fail_save(*_args, before_first_click, **_kwargs):
        if click_started:
            before_first_click()
        raise RuntimeError("browser drift")

    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard", fail_save)

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        title="AI Team Lead",
        specialization=["Руководитель группы разработки"],
        salary=None,
        currency=None,
        employment=None,
        work_format=None,
        commute=None,
        business_trips=None,
        mode=None,
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    out = capsys.readouterr().out
    assert ("(uncertain)" in out) is click_started

    row = History(history_path).command_runs()[-1]
    assert row["attempted"] == row[expected_status] == 1
    other = "failed" if expected_status == "uncertain" else "uncertain"
    assert row[other] == row["success"] == row["skipped"] == 0


def test_edit_experience_hard_failure_wins_over_uncertain_in_same_batch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for the same batch-classification bug (round 2 fixed
    it in edit_education.py; round 3 found it still present here): a hard
    failure on one --entry item must win over an uncertain result on another
    item in the same batch.
    """
    import hhru_bot.commands.edit_experience as command
    from hhru_bot.experience import ExperienceResult

    resume = SimpleNamespace(id="r1", resume_id="r1")
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr("hhru_bot.experience.read_experience_on_hh", lambda *_a, **_kw: [])
    monkeypatch.setattr("hhru_bot.copy_resume.list_resume_cards", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "hhru_bot.experience.edit_experience_on_hh",
        lambda *_a, **_kw: [
            ExperienceResult("строка 1: отклонено, ошибка формы"),
            ExperienceResult("строка 2: не подтверждено", uncertain=True),
        ],
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        mode="fill",
        career=None,
        existing=None,
        entry=[
            '{"company": "a", "position": "b", "start_month": "1"}',
            '{"company": "c", "position": "d", "start_month": "2"}',
        ],
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_experience"
    assert row["attempted"] == row["failed"] == 1
    assert row["uncertain"] == row["success"] == row["skipped"] == 0


def test_edit_skills_acted_failure_is_recorded_as_uncertain(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for a BLOCKING finding in cycle-review round 3 of PR
    #472: edit_skills.py had no uncertain lane at all — SkillsResult(
    success=False, acted=True) was always counted as failed, even though
    ``acted`` documents that the click may already have reached hh.ru.
    """
    import hhru_bot.commands.edit_skills as command
    from hhru_bot.skills import SkillsResult

    resume = SimpleNamespace(id="r1", resume_id="r1")
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.skills.edit_skills_on_hh",
        lambda *_a, **_kw: SkillsResult(
            success=False, acted=True, reason="сохранение навыков не подтверждено"
        ),
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        mode="append",
        skill=["Python=advanced"],
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "(uncertain)" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_skills"
    assert row["attempted"] == row["uncertain"] == 1
    assert row["failed"] == row["success"] == row["skipped"] == 0


def test_edit_languages_acted_failure_is_recorded_as_uncertain(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Regression test for a BLOCKING finding in cycle-review round 3 of PR
    #472: edit_languages.py had no uncertain lane at all — LanguagesResult(
    success=False, acted=True) was always counted as failed.
    """
    import hhru_bot.commands.edit_languages as command
    from hhru_bot.languages import Language, LanguagesResult

    resume = SimpleNamespace(id="r1", resume_id="r1")
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: object())

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.languages.edit_languages_on_hh",
        lambda *_a, **_kw: LanguagesResult(
            success=False,
            acted=True,
            proposed=(Language("English", "B1"),),
            reason="сохранение не подтверждено",
        ),
    )

    history_path = tmp_path / "history.db"
    args = argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        mode="append",
        language=["English=B1"],
        dry_run=False,
        force=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    assert "(uncertain)" in capsys.readouterr().out

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "edit_languages"
    assert row["attempted"] == row["uncertain"] == 1
    assert row["failed"] == row["success"] == row["skipped"] == 0


@pytest.mark.parametrize(
    "module_name",
    ["edit_education", "edit_experience", "edit_skills", "edit_languages", "resume_position"],
)
def test_browser_launch_error_propagates_to_cli_environment_handler(
    tmp_path: Path, monkeypatch, module_name: str
) -> None:
    """Regression test for a finding in cycle-review round 3 of PR #472: a
    broad ``except Exception`` added around launch_context in each command
    must not swallow BrowserLaunchError before it reaches cli.py's dedicated
    handler (prints "[ENVIRONMENT] ..." and exits distinctly from an
    ordinary command failure).
    """
    from hhru_bot.browser import BrowserLaunchError

    command = importlib.import_module(f"hhru_bot.commands.{module_name}")
    resume = SimpleNamespace(
        id="r1",
        resume_id="r1",
        resume_url="https://hh.ru/resume/r1",
        education=None,
        ai_profile=object(),
    )
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=object())
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    def exploding_launch_context(*_args, **_kwargs):
        raise BrowserLaunchError("CODEX_SANDBOX_BROWSER_FAILURE: sandboxed")

    monkeypatch.setattr("hhru_bot.browser.launch_context", exploding_launch_context)

    history_path = tmp_path / "history.db"
    base_args = {
        "config": "config.yaml",
        "headless": True,
        "resume": "r1",
        "dry_run": False,
        "force": True,
        "history": str(history_path),
    }
    per_command_args = {
        "edit_education": {
            "section": "both",
            "source": None,
            "mode": None,
            "institution": "МГУ",
            "faculty": None,
            "specialty": None,
            "year": None,
            "primary_entry": None,
            "additional_entry": None,
        },
        "edit_experience": {
            "mode": "fill",
            "career": None,
            "existing": None,
            "entry": ['{"company": "a", "position": "b", "start_month": "1"}'],
        },
        "edit_skills": {"mode": "append", "skill": ["Python=advanced"]},
        "edit_languages": {"mode": "append", "language": ["English=B1"]},
        "resume_position": {
            "title": "новая",
            "specialization": None,
            "salary": None,
            "currency": None,
            "employment": None,
            "work_format": None,
            "commute": None,
            "business_trips": None,
            "mode": None,
        },
    }
    args = argparse.Namespace(**base_args, **per_command_args[module_name])

    with pytest.raises(BrowserLaunchError):
        command.run(args)

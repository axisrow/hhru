"""Durable-ledger wiring for the single-mutation resume edit commands (#465)."""

from __future__ import annotations

import argparse
import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hhru_bot.history import History

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
        "hhru_bot.resume_position.apply_position",
        lambda *_args, **_kwargs: None,
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

    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard_minimum", fail_save)

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
        allow_auto_publish=True,
        history=str(history_path),
    )

    assert command.run(args) is True
    out = capsys.readouterr().out
    assert ("(uncertain)" in out) is click_started

    row = History(history_path).command_runs()[-1]
    assert row["attempted"] == row[expected_status] == 1
    other = "failed" if expected_status == "uncertain" else "uncertain"
    assert row[other] == row["success"] == row["skipped"] == 0


def _draft_position_args(history_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config="config.yaml",
        headless=True,
        resume="r1",
        title="Python-разработчик",
        specialization=["Программист, разработчик"],
        salary=None,
        currency=None,
        employment=None,
        work_format=None,
        commute=None,
        business_trips=None,
        mode=None,
        dry_run=False,
        force=True,
        allow_auto_publish=True,
        history=str(history_path),
    )


class _WizardSavePage:
    """Minimal page double for the #913 wizard write paths: URL carrier with
    a single always-clickable locator (editor SAVE in the fallback fixup)."""

    url = "https://hh.ru/profile/resume/professional_role?resume=r1"

    def locator(self, _selector):
        class _Locator:
            def count(self):
                return 1

            def click(self):
                return None

            def wait_for(self, *, state=None, timeout=None):
                return None

        return _Locator()


def test_chip_popular_unavailable_falls_back_to_wizard_minimum_and_succeeds(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """#890: when the chip-popular shape cannot save the exact catalog leaf,
    the command must save a throwaway placeholder via
    ``save_position_wizard_minimum``, confirm it via
    ``verify_wizard_minimum_save``, reopen the form as an editor, and finish
    through the existing ``apply_position``/SAVE path — recording exactly
    ONE durable attempt as success, not two attempts and not uncertain.
    """
    import hhru_bot.commands.resume_position as command
    from hhru_bot.professional_roles import ProfessionalRole
    from hhru_bot.resume_position import (
        ChipPopularUnavailable,
        PositionFlowContext,
        PositionValues,
    )
    from hhru_bot.resume_state import ResumeState

    resume = SimpleNamespace(id="r1", resume_id="r1", ai_profile=None)
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)

    class _Locator:
        def count(self):
            return 1

        def click(self):
            return None

        def wait_for(self, *, state=None, timeout=None):
            return None

    class _Page:
        url = "https://hh.ru/profile/resume/professional_role?resume=r1"

        def locator(self, _selector):
            return _Locator()

    page = _Page()

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: page)

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)

    flows = iter(
        [
            PositionFlowContext(
                "wizard",
                "r1",
                PositionValues(title="Python-разработчик"),
                ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
            ),
            # second call: re-bind right before WRITE, still wizard
            PositionFlowContext(
                "wizard",
                "r1",
                PositionValues(title="Python-разработчик"),
                ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
            ),
            # third call: after wizard-minimum, the draft is now an editor
            PositionFlowContext(
                "editor",
                "r1",
                PositionValues(title="Администратор"),
                ResumeState(status="not_finished"),
            ),
        ]
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form", lambda _page, _resume: next(flows)
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.is_position_wizard", lambda _page, _resume_id: True
    )
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda _page, label: ProfessionalRole("96", label, "ИТ"),
    )

    def fail_with_chip_popular(*_args, before_first_click, **_kwargs):
        before_first_click()
        raise ChipPopularUnavailable("chip-popular не содержит нужную специализацию")

    minimum_calls: list[bool] = []

    def fake_minimum(_page, _resume, *, before_first_click=None):
        minimum_calls.append(True)
        return "Администратор"

    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard", fail_with_chip_popular)
    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard_minimum", fake_minimum)
    monkeypatch.setattr(
        "hhru_bot.resume_position.verify_wizard_minimum_save",
        lambda _page, _resume: ResumeState(status="not_finished"),
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.verify_wizard_save",
        lambda *_args, **_kwargs: ResumeState(status="not_finished"),
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.apply_position",
        lambda *_args, **_kwargs: None,
    )

    history_path = tmp_path / "history.db"
    assert command.run(_draft_position_args(history_path)) is False
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert minimum_calls == [True]

    row = History(history_path).command_runs()[-1]
    assert row["command"] == "resume_position"
    assert row["attempted"] == row["success"] == 1
    assert row["failed"] == row["uncertain"] == row["skipped"] == 0


def test_exact_leaf_title_is_saved_directly_in_wizard(tmp_path: Path, monkeypatch, capsys) -> None:
    """#913: when the requested title IS the exact catalog leaf (title ==
    agreed specialization), the command must save it directly through the
    wizard's own catalog modal — no placeholder, no editor fixup. The direct
    path is proven by battle run #911 (5487694535): the wizard writes the
    real role_id in one pass and the readback verifies title+role together.
    """
    import hhru_bot.commands.resume_position as command
    from hhru_bot.professional_roles import ProfessionalRole
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    resume = SimpleNamespace(id="r1", resume_id="r1", ai_profile=None)
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)
    monkeypatch.setattr(
        "hhru_bot.resume_position.is_position_wizard", lambda _page, _resume_id: True
    )
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda _page, label: ProfessionalRole("124", label, "Информационные технологии"),
    )

    page = SimpleNamespace(url="https://hh.ru/profile/resume/professional_role?resume=r1")

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: page)

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)

    flow = PositionFlowContext(
        "wizard",
        "r1",
        PositionValues(title="Тестировщик"),
        ResumeState(status="not_finished", next_incomplete_screen_id="common"),
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda _page, _resume, **_kwargs: flow,
    )

    direct_calls: list[dict] = []

    def fake_direct(_page, _resume, plan, *, role_id, before_first_click=None):
        direct_calls.append({"title": plan.title, "role_id": role_id})
        return None

    minimum_calls: list[bool] = []

    def fake_minimum(_page, _resume, *, before_first_click=None):
        minimum_calls.append(True)
        return "Администратор"

    verify_calls: list[dict] = []

    def fake_verify(_page, _resume, *, expected_title, expected_role_id, expected_role_label):
        verify_calls.append(
            {"title": expected_title, "role_id": expected_role_id, "label": expected_role_label}
        )
        return ResumeState(status="not_finished")

    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard", fake_direct)
    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard_minimum", fake_minimum)
    monkeypatch.setattr(
        "hhru_bot.resume_position.verify_wizard_save",
        fake_verify,
    )
    apply_calls: list[bool] = []
    monkeypatch.setattr(
        "hhru_bot.resume_position.apply_position",
        lambda *_a, **_k: apply_calls.append(True),
    )

    args = _draft_position_args(tmp_path / "history.db")
    args.title = "Тестировщик"
    args.specialization = ["Тестировщик"]
    assert command.run(args) is False
    out = capsys.readouterr().out
    assert "[OK]" in out

    assert direct_calls == [{"title": "Тестировщик", "role_id": "124"}]
    assert minimum_calls == []  # прямому пути заглушка не нужна
    assert apply_calls == []  # editor-фиксап не выполнялся
    assert verify_calls == [{"title": "Тестировщик", "role_id": "124", "label": "Тестировщик"}]

    row = History(tmp_path / "history.db").command_runs()[-1]
    assert row["attempted"] == row["success"] == 1
    assert row["failed"] == row["uncertain"] == 0


def test_landed_first_next_save_is_verified_not_masked_by_fallback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """#899 (регресс-страж): когда первый NEXT прямого пути сам сохраняет
    специализацию и закрывает экран — живой факт #893 (hh.ru редиректит
    завершённый professional_role; замер воспроизведён на двух резюме) и
    #900-инцидент — команда обязана подтвердить сохранение через
    ``verify_wizard_save`` и записать success: НЕ запускать
    wizard-minimum fallback по «виду экрана» и НЕ рапортовать (uncertain).
    Здесь гоняется НАСТОЯЩАЯ ``save_position_wizard`` (не двойник): модалка
    не подтверждена ни разу, URL уходит с визарда первым же NEXT.
    """
    import hhru_bot.commands.resume_position as command
    import hhru_bot.resume_position as resume_position_module
    from hhru_bot.professional_roles import ProfessionalRole
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    resume = SimpleNamespace(id="r1", resume_id="r1", ai_profile=None)
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)
    monkeypatch.setattr(
        "hhru_bot.resume_position.is_position_wizard", lambda _page, _resume_id: True
    )
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda _page, label: ProfessionalRole("96", label, "ИТ"),
    )

    class _Page:
        def __init__(self) -> None:
            self.url = "https://hh.ru/profile/resume/professional_role?resume=r1"

        def locator(self, selector):
            return {
                resume_position_module.WIZARD_POSITION: _PositionLocator(),
                resume_position_module.WIZARD_POSITION_CLEAR: _CountLocator(0),
                resume_position_module.WIZARD_NEXT: _NextLocator(self),
            }[selector]

        def wait_for_timeout(self, _ms):
            # Транзитный chip-экран: URL уходит не мгновенно по клику, а
            # через один тик опроса — живой зазор, в который стреляла
            # диагностика «по виду экрана» (#892/#899).
            self.url = "https://hh.ru/resume/r1"
            return None

    class _PositionLocator:
        def count(self):
            return 1

        def input_value(self):
            return ""

        def fill(self, _value):
            return None

    class _CountLocator:
        def __init__(self, count: int) -> None:
            self._count = count

        def count(self):
            return self._count

    class _NextLocator:
        def __init__(self, page: _Page) -> None:
            self._page = page
            self.first = self

        def count(self):
            return 1

        def wait_for(self, *, state=None, timeout=None):
            return None

        def click(self):
            # Единственный NEXT живого сценария #899: hh.ru принимает
            # специализацию и закрывает экран; сам URL уходит позже, тиком
            # опроса (см. _Page.wait_for_timeout).
            return None

    page = _Page()

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: page)

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)

    flow = PositionFlowContext(
        "wizard",
        "r1",
        PositionValues(title="Программист, разработчик"),
        ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
    )
    open_calls: list[bool] = []

    def fake_open(_page, _resume, **_kwargs):
        open_calls.append(True)
        return flow

    monkeypatch.setattr("hhru_bot.resume_position.open_position_form", fake_open)

    # Модалка каталога не подтверждена ни разу — как в живом прогоне #899:
    # экран закрылся прямым save, без открытия «Уточните специальность».
    monkeypatch.setattr(
        "hhru_bot.resume_position.is_profession_modal_confirmed", lambda _page: False
    )
    monkeypatch.setattr("hhru_bot.resume_position.dismiss_cookie_banner", lambda _page: None)
    select = MagicMock()
    monkeypatch.setattr("hhru_bot.resume_position.select_wizard_catalog_leaf", select)
    monkeypatch.setattr("hhru_bot.resume_position._dump_wizard_failure", lambda *_args: "dump.html")

    minimum = MagicMock()
    minimum_verify = MagicMock()
    apply = MagicMock()
    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard_minimum", minimum)
    monkeypatch.setattr("hhru_bot.resume_position.verify_wizard_minimum_save", minimum_verify)
    monkeypatch.setattr("hhru_bot.resume_position.apply_position", apply)

    verify_calls: list[dict] = []

    def fake_verify(_page, _resume, *, expected_title, expected_role_id, expected_role_label):
        verify_calls.append(
            {"title": expected_title, "role_id": expected_role_id, "label": expected_role_label}
        )
        return ResumeState(status="not_finished")

    monkeypatch.setattr("hhru_bot.resume_position.verify_wizard_save", fake_verify)

    args = _draft_position_args(tmp_path / "history.db")
    args.title = "Программист, разработчик"
    assert command.run(args) is False
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "(uncertain)" not in out

    # Состоявшееся сохранение подтверждается readback'ом, а не маскируется
    # fallback'ом: ни заглушки wizard-minimum, ни editor-фиксапа. Открытий
    # формы ровно два — вход и обязательный pre-WRITE re-bind прямого пути
    # (commands/resume_position.py:378); третий вызыв был бы реентерой
    # fallback'а после состоявшегося save.
    minimum.assert_not_called()
    minimum_verify.assert_not_called()
    apply.assert_not_called()
    select.assert_not_called()
    assert len(open_calls) == 2
    assert verify_calls == [
        {"title": "Программист, разработчик", "role_id": "96", "label": "Программист, разработчик"}
    ]

    row = History(tmp_path / "history.db").command_runs()[-1]
    assert row["attempted"] == row["success"] == 1
    assert row["failed"] == row["uncertain"] == row["skipped"] == 0


def test_direct_save_is_never_attempted_in_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    """#909/#913: dry-run строго немутирующ — прямой путь (как и фолбэк)
    не выполняется, wizard не открывается при явной --specialization."""
    import hhru_bot.commands.resume_position as command
    from hhru_bot.professional_roles import ProfessionalRole
    from hhru_bot.resume_position import PositionFlowContext, PositionValues
    from hhru_bot.resume_state import ResumeState

    resume = SimpleNamespace(id="r1", resume_id="r1", ai_profile=None)
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda _page, label: ProfessionalRole("124", label, "Информационные технологии"),
    )

    page = SimpleNamespace(url="https://hh.ru/profile/resume/professional_role?resume=r1")

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: page)

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda _page, _resume, **_kwargs: PositionFlowContext(
            "wizard",
            "r1",
            PositionValues(title="Тестировщик"),
            ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
        ),
    )
    direct = MagicMock()
    minimum = MagicMock()
    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard", direct)
    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard_minimum", minimum)

    args = _draft_position_args(tmp_path / "history.db")
    args.title = "Тестировщик"
    args.specialization = ["Тестировщик"]
    args.dry_run = True
    assert command.run(args) is False
    out = capsys.readouterr().out
    assert "Ничего не записано" in out
    direct.assert_not_called()
    minimum.assert_not_called()


@pytest.mark.parametrize(
    ("title", "expect_direct"),
    [("Тестировщик", True), ("тестировщик", False)],
)
def test_direct_save_chip_popular_still_falls_back_to_wizard_minimum(
    title: str, expect_direct: bool, tmp_path: Path, monkeypatch, capsys
) -> None:
    """#913: the direct path is tried first for an exact leaf, but when the
    catalog modal never confirms (chip-popular shape без каталога, #881) it
    must fall back to the existing wizard-minimum + editor fixup — recording
    ONE durable success attempt, not an uncertain result and not a second
    begin_attempt(). The lowercase variant (review of PR #914) must not even
    enter the direct path (gate is byte-exact like the readback).
    """
    import hhru_bot.commands.resume_position as command
    from hhru_bot.professional_roles import ProfessionalRole
    from hhru_bot.resume_position import (
        ChipPopularUnavailable,
        PositionFlowContext,
        PositionValues,
    )
    from hhru_bot.resume_state import ResumeState

    resume = SimpleNamespace(id="r1", resume_id="r1", ai_profile=None)
    config = SimpleNamespace(storage_state_file="session.json", user_agent=None, ai=None)
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _path: config)
    monkeypatch.setattr("hhru_bot.commands._common.resolve_resume", lambda *_a, **_kw: resume)
    monkeypatch.setattr(
        "hhru_bot.resume_position.is_position_wizard", lambda _page, _resume_id: True
    )
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda _page, label: ProfessionalRole("124", label, "Информационные технологии"),
    )

    page = _WizardSavePage()

    @contextmanager
    def fake_launch_context(*_args, **_kwargs):
        yield SimpleNamespace(new_page=lambda: page)

    monkeypatch.setattr("hhru_bot.browser.launch_context", fake_launch_context)

    flows = iter(
        [
            PositionFlowContext(
                "wizard",
                "r1",
                PositionValues(title="Тестировщик"),
                ResumeState(status="not_finished", next_incomplete_screen_id="common"),
            ),
            PositionFlowContext(
                "wizard",
                "r1",
                PositionValues(title="Тестировщик"),
                ResumeState(status="not_finished", next_incomplete_screen_id="common"),
            ),
            PositionFlowContext(
                "editor",
                "r1",
                PositionValues(title="Тестировщик"),
                ResumeState(status="not_finished", next_incomplete_screen_id="common"),
            ),
        ]
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.open_position_form",
        lambda _page, _resume, **_kwargs: next(flows),
    )

    direct_calls: list[bool] = []

    def fail_with_chip_popular(_page, _resume, _plan, *, role_id, before_first_click=None):
        # Модалка каталога не подтвердилась уже ПОСЛЕ первого NEXT —
        # прямому пути больше нечего пробовать без повторного клика.
        direct_calls.append(True)
        before_first_click()
        raise ChipPopularUnavailable("модалка каталога не подтвердилась")

    minimum_calls: list[bool] = []

    def fake_minimum(_page, _resume, *, before_first_click=None):
        minimum_calls.append(True)
        return "Администратор"

    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard", fail_with_chip_popular)
    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard_minimum", fake_minimum)
    monkeypatch.setattr(
        "hhru_bot.resume_position.verify_wizard_minimum_save",
        lambda _page, _resume: ResumeState(status="not_finished"),
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.verify_wizard_save",
        lambda *_args, **_kwargs: ResumeState(status="not_finished"),
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.apply_position",
        lambda *_args, **_kwargs: None,
    )

    args = _draft_position_args(tmp_path / "history.db")
    args.title = title
    args.specialization = ["Тестировщик"]
    assert command.run(args) is False
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert minimum_calls == [True]
    assert bool(direct_calls) is expect_direct


def test_chip_popular_unavailable_fallback_failure_is_uncertain_not_double_counted(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """If the wizard-minimum fallback itself fails, the whole two-stage
    attempt must still record exactly ONE durable attempt as uncertain — not
    failed, and not a second `begin_attempt()` for the fallback stage.
    """
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
            "r1",
            PositionValues(title="Python-разработчик"),
            ResumeState(status="not_finished", next_incomplete_screen_id="professional_role"),
        ),
    )
    monkeypatch.setattr(
        "hhru_bot.resume_position.is_position_wizard", lambda _page, _resume_id: True
    )
    monkeypatch.setattr(
        "hhru_bot.professional_roles.resolve_explicit_role",
        lambda _page, label: ProfessionalRole("96", label, "ИТ"),
    )

    def fail_minimum(*_args, before_first_click=None, **_kwargs):
        before_first_click()
        raise RuntimeError("wizard-minimum тоже не сработал")

    monkeypatch.setattr("hhru_bot.resume_position.save_position_wizard_minimum", fail_minimum)

    history_path = tmp_path / "history.db"
    assert command.run(_draft_position_args(history_path)) is True
    out = capsys.readouterr().out
    assert "(uncertain)" in out

    row = History(history_path).command_runs()[-1]
    assert row["attempted"] == row["uncertain"] == 1
    assert row["failed"] == row["success"] == row["skipped"] == 0


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

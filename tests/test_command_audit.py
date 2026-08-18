"""Shared audit primitives for dangerous CLI mutations (#308)."""

from __future__ import annotations

import pytest

from hhru_bot.commands._audit import action_status, record_resume_action
from hhru_bot.history import History

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("dry_run", "success", "uncertain", "expected"),
    [
        (True, True, False, "dry_run"),
        (True, False, True, "dry_run"),
        (False, True, False, "success"),
        (False, False, False, "failed"),
        (False, False, True, "uncertain"),
    ],
)
def test_action_status_preserves_uncertain_after_a_live_click(
    dry_run, success, uncertain, expected
):
    assert action_status(dry_run=dry_run, success=success, uncertain=uncertain) == expected


def test_record_resume_action_uses_resume_as_the_audit_scope(tmp_path):
    history = History(tmp_path / "history.db")
    record_resume_action(
        history, "resume-1", "copy_resume", "uncertain", "click may have reached hh.ru"
    )

    with history._connect() as conn:
        row = conn.execute("SELECT resume_id, vacancy_id, action, status FROM actions").fetchone()
    assert tuple(row) == ("resume-1", "resume-1", "copy_resume", "uncertain")

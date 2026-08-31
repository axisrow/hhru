"""Safety gate for resume-position auto-publication (#900)."""

from types import SimpleNamespace

import pytest

from hhru_bot.commands.resume_position import _professional_role_closes_resume
from hhru_bot.resume_state import ResumeState

pytestmark = pytest.mark.unit


def _flow(kind: str, next_screen: str | None):
    return SimpleNamespace(kind=kind, state=ResumeState(next_incomplete_screen_id=next_screen))


def test_gate_blocks_only_last_professional_role_screen():
    assert _professional_role_closes_resume(_flow("wizard", "professional_role"))


def test_gate_allows_other_incomplete_screen_and_editor():
    assert not _professional_role_closes_resume(_flow("wizard", "education"))
    assert not _professional_role_closes_resume(_flow("editor", "professional_role"))

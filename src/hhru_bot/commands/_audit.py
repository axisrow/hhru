"""Narrow audit helpers shared by dangerous resume mutations."""

from __future__ import annotations

from ..history import History


def action_status(*, dry_run: bool, success: bool, uncertain: bool = False) -> str:
    """Map a confirmed mutation outcome to the history status vocabulary.

    ``uncertain`` takes precedence over a failed result after a live click: the
    request may have reached hh.ru and must not be reported as a safe failure.
    Dry-runs never click, so their explicit status remains first.
    """
    if dry_run:
        return "dry_run"
    if uncertain:
        return "uncertain"
    return "success" if success else "failed"


def record_resume_action(
    history: History, resume_id: str, action: str, status: str, reason: str
) -> None:
    """Record a real or uncertain action for exactly one configured resume.

    A dry-run is a preview, not an action.  Keep the guard here so all resume
    mutation commands share the same no-side-effect history contract.
    """
    if status == "dry_run":
        return
    history.record_action(resume_id, resume_id, action, status, reason)

"""Browser step for changing resume visibility (#566).

This command is intentionally a dry-run-only preview until the visibility
editor selectors are confirmed on live hh.ru DOM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .selector_groups.resume_visibility import (
    RESUME_VISIBILITY_MODE_CONTROL,
    RESUME_VISIBILITY_SAVE,
)


@dataclass
class ResumeVisibilityResult:
    resume_id: str
    success: bool
    reason: str
    uncertain: bool = False


def set_resume_visibility_on_hh(
    page,  # noqa: ANN001 - kept compatible with the Playwright command seam
    resume,
    mode: str,
    dry_run: bool,
    *,
    before_click: Callable[[], None] | None = None,
) -> ResumeVisibilityResult:
    """Preview or refuse a visibility change until its UI contract is known."""
    del page, before_click
    if dry_run:
        return ResumeVisibilityResult(
            resume.resume_id,
            True,
            f"dry-run; видимость будет изменена на «{mode}»",
        )
    if not RESUME_VISIBILITY_MODE_CONTROL or not RESUME_VISIBILITY_SAVE:
        return ResumeVisibilityResult(
            resume.resume_id,
            False,
            "селектор блока видимости не подтверждён живым DOM; запись запрещена",
        )
    return ResumeVisibilityResult(
        resume.resume_id, False, "поток изменения видимости не подтверждён живым DOM"
    )

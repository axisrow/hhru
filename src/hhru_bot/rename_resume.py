"""Browser step for renaming a resume (#522).

The target field is deliberately fail-closed until its selector is confirmed
in a live authenticated hh.ru DOM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .selector_groups.resume_rename import RESUME_NAME_INPUT, RESUME_NAME_SAVE


@dataclass
class RenameResumeResult:
    resume_id: str
    success: bool
    reason: str
    uncertain: bool = False


def rename_resume_on_hh(
    page,  # noqa: ANN001 - kept compatible with the Playwright command seam
    resume,
    name: str,
    dry_run: bool,
    *,
    before_click: Callable[[], None] | None = None,
) -> RenameResumeResult:
    """Rename exactly one resume, or refuse while the UI contract is unknown."""
    del page, before_click
    if dry_run:
        return RenameResumeResult(
            resume.resume_id, True, f"dry-run; имя будет изменено на «{name}»"
        )
    if not RESUME_NAME_INPUT or not RESUME_NAME_SAVE:
        return RenameResumeResult(
            resume.resume_id,
            False,
            "селектор названия резюме не подтверждён живым DOM; запись запрещена",
        )
    # This branch is intentionally unreachable until the selectors above are
    # confirmed and the identity-bound UI flow is implemented.
    return RenameResumeResult(
        resume.resume_id, False, "поток переименования не подтверждён живым DOM"
    )

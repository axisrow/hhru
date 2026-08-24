"""Selectors for changing resume visibility.

The visibility editor has not been confirmed in an authenticated live DOM
(issue #566).  Keep the selectors unavailable until that investigation is
complete; callers must fail closed rather than guess at a personal-data
control.
"""

from __future__ import annotations

# НЕ ПОДТВЕРЖДЕНО живым DOM: do not replace these with guessed selectors.
RESUME_VISIBILITY_MODE_CONTROL: str | None = None
RESUME_VISIBILITY_SAVE: str | None = None

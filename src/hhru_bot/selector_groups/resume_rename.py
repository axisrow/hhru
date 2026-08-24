"""Selectors for renaming a resume.

The name editor has not yet been confirmed in an authenticated live DOM
(issue #522).  Keep the selectors explicitly unavailable so a future caller
cannot accidentally write to a similarly-looking field.
"""

from __future__ import annotations

# НЕ ПОДТВЕРЖДЕНО живым DOM: the resume-list name editor may be a modal or a
# field on the resume editor.  Do not replace these with guessed selectors.
RESUME_NAME_INPUT: str | None = None
RESUME_NAME_SAVE: str | None = None

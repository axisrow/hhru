"""Confirmed selectors for the experience editor (issue #261).

The editor is not ``/resume/{id}/edit``.  Existing rows are opened from the
resume page and the form lives at ``/profile/edit/experience/{rowId}``.

The FIRST row on a resume with zero experience entries is a distinct DOM
shape (#786/#787, live write-confirmed 2026-08-29): it is not reachable
through ``EXPERIENCE_ADD_BUTTON`` (never confirmed — see below) nor through
the visible "Опыт работы" suggestion chip, which navigates to the *shared*
``/profile/edit/experience`` editor without a reliable ``resumeFrom``
binding on an incomplete resume. The confirmed path is a direct navigation
to ``/resume/edit/{resume_id}/experience``, which opens the form pre-bound
to that resume with no click needed — its field/button ``data-qa`` values
are a separate namespace (``resume-editor-experience-*``,
``resume-partial-edit-*``) from the indexed row editor above.
"""

from __future__ import annotations

from ._generated import optional_selector as _optional_selector
from ._generated import selector as _selector

EXPERIENCE_EDIT_BUTTON = _selector("resume_experience.EXPERIENCE_EDIT_BUTTON")
EXPERIENCE_COMPANY = _selector("resume_experience.EXPERIENCE_COMPANY")
EXPERIENCE_POSITION = _selector("resume_experience.EXPERIENCE_POSITION")
EXPERIENCE_COMPANY_URL = _selector("resume_experience.EXPERIENCE_COMPANY_URL")
EXPERIENCE_START_YEAR = _selector("resume_experience.EXPERIENCE_START_YEAR")
EXPERIENCE_END_YEAR = _selector("resume_experience.EXPERIENCE_END_YEAR")
EXPERIENCE_DESCRIPTION = _selector("resume_experience.EXPERIENCE_DESCRIPTION")
EXPERIENCE_CANCEL = _selector("resume_experience.EXPERIENCE_CANCEL")
EXPERIENCE_SAVE = _selector("resume_experience.EXPERIENCE_SAVE")

# The add trigger is intentionally a candidate: unlike the row/editor fields,
# it was not present in the read-only research dump.  Code must require one
# unambiguous match and fail closed rather than click a text button by guess.
EXPERIENCE_ADD_BUTTON = _optional_selector("resume_experience.EXPERIENCE_ADD_BUTTON")

# First-row editor at /resume/edit/{resume_id}/experience (#786/#787, live
# write-confirmed 2026-08-25/29 on a resume with zero experience entries).
FIRST_EXPERIENCE_COMPANY = _selector("resume_experience.FIRST_EXPERIENCE_COMPANY")
FIRST_EXPERIENCE_POSITION = _selector("resume_experience.FIRST_EXPERIENCE_POSITION")
FIRST_EXPERIENCE_SAVE = _selector("resume_experience.FIRST_EXPERIENCE_SAVE")
FIRST_EXPERIENCE_CANCEL = _selector("resume_experience.FIRST_EXPERIENCE_CANCEL")

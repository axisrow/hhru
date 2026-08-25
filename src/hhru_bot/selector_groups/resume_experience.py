"""Confirmed selectors for the experience editor (issue #261).

The editor is not ``/resume/{id}/edit``.  Existing rows are opened from the
resume page and the form lives at ``/profile/edit/experience/{rowId}``.
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

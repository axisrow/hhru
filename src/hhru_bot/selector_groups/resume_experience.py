"""Confirmed selectors for the experience editor (issue #261).

The editor is not ``/resume/{id}/edit``.  Existing rows are opened from the
resume page and the form lives at ``/profile/edit/experience/{rowId}``.
"""

from __future__ import annotations

EXPERIENCE_EDIT_BUTTON = "[data-qa='edit-experience-button-{index}']"
EXPERIENCE_COMPANY = "[data-qa='resume-profile-experience-specific-company-input-{index}']"
EXPERIENCE_POSITION = "[data-qa='resume-profile-experience-specific-position-input-{index}']"
EXPERIENCE_COMPANY_URL = "[data-qa='resume-editor-experience-company-url-input']"
EXPERIENCE_START_YEAR = "[data-qa='resume-editor-experience-start-year-input']"
EXPERIENCE_END_YEAR = "[data-qa='resume-editor-experience-end-year-input']"
EXPERIENCE_DESCRIPTION = "[data-qa='resume-editor-experience-description-input']"
EXPERIENCE_CANCEL = "[data-qa='profile-layout-cancel-button']"
EXPERIENCE_SAVE = "[data-qa='profile-layout-save-button']"

# The add trigger is intentionally a candidate: unlike the row/editor fields,
# it was not present in the read-only research dump.  Code must require one
# unambiguous match and fail closed rather than click a text button by guess.
EXPERIENCE_ADD_BUTTON = "[data-qa='resume-profile-experience-add-button']"

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

# The historical candidate ``resume-profile-experience-add-button`` does not
# exist on hh.ru (live probe 2026-08-29 and 2026-08-30, count=0 on resumes with
# and without experience).  The real "Добавить" control is scoped inside the
# experience card and carries the shared ``data-qa='link'`` — the same shape as
# ``resume_education.PRIMARY_ADD``/``ADDITIONAL_ADD``.  It stays optional and is
# not used yet — the first row is created by direct navigation (see above) — and
# any future caller must still require one unambiguous match, because the
# generic ``link`` value is only meaningful together with the card scope.
EXPERIENCE_ADD_BUTTON = _optional_selector("resume_experience.EXPERIENCE_ADD_BUTTON")

# First-row editor at /resume/edit/{resume_id}/experience (#786/#787, live
# write-confirmed 2026-08-25/29 on a resume with zero experience entries).
FIRST_EXPERIENCE_COMPANY = _selector("resume_experience.FIRST_EXPERIENCE_COMPANY")
FIRST_EXPERIENCE_POSITION = _selector("resume_experience.FIRST_EXPERIENCE_POSITION")
FIRST_EXPERIENCE_SAVE = _selector("resume_experience.FIRST_EXPERIENCE_SAVE")
FIRST_EXPERIENCE_CANCEL = _selector("resume_experience.FIRST_EXPERIENCE_CANCEL")

# #800: "Работаю сейчас" checkbox above the end-date fields (confirmed live
# 2026-08-30 on /resume/edit/{resume_id}/experience, draft resume). Checked
# by default on a new entry — while checked, the end-year/end-month controls
# are disabled. The underlying magritte checkbox component's own data-qa
# ("checkbox") is a generic, non-unique token; it was confirmed to resolve to
# exactly one match on this form (count()==1), so it is used as-is rather
# than scoped further.
FIRST_EXPERIENCE_CURRENT_CHECKBOX = _selector("resume_experience.FIRST_EXPERIENCE_CURRENT_CHECKBOX")

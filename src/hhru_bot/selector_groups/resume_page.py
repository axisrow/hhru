"""Страница резюме (/resume/{hash}).

На живом DOM issue #225 кнопка публикации не появилась: свежая копия была
заблокирована незавершённым шагом ``professional_role``. Селекторы публикации
сохраняются как fail-closed кандидаты до отдельного подтверждения на готовом
к публикации черновике.
"""

from __future__ import annotations

# Existing bump selectors (confirmed by the bump feature's live check).
RESUME_BUMP_BUTTON = "[data-qa='resume-update-button']"
RESUME_BUMP_DISABLED_HINT = "[data-qa='resume-update-button-disabled']"

# Кандидаты из исходного флоу (#219); на заблокированной копии #225 ни один не
# присутствовал в живом DOM. Команда обязана проверять count и не угадывать.
RESUME_PUBLISH_BUTTON = "button:has-text('Опубликовать')"
RESUME_PUBLISH_BUTTON_DATA_QA = "[data-qa='resume-publish']"
# Только read-only сообщение о текущей видимости; команда его не нажимает.
RESUME_VISIBILITY_BUTTON = "button:has-text('Изменить видимость')"

# Inline editor selectors confirmed by the authenticated read-only research in
# issue #268.  NOTE: #268 called this section inline (no /edit route), but #328
# found the same claim false for the neighbouring position/skills editors on
# this same profile page — this claim is unaudited against #328's finding and
# should not be trusted until re-verified on live DOM (see #328 follow-up).
RESUME_EDIT_ABOUT_BUTTON = "[data-qa='resume-edit-button-about']"
RESUME_ABOUT_EDITOR = "[data-qa='resume-editor-about']"
RESUME_ABOUT_NO_EXPERIENCE_REASON = "[data-qa^='resume-editor-about-no-experience-reason-']"

# The profile-page control routes to /resume/edit/{id}/keySkills before the
# editor is mounted (authenticated live audit, #328).
RESUME_SKILLS_EDIT_BUTTON = "[data-qa='skills-add']"
RESUME_SKILLS_INPUT = "[data-qa='resume-editor-skills-input']"
RESUME_SKILLS_CHIP_INPUT = "[data-qa='chips-trigger-input']"
RESUME_SKILLS_RECOMMENDED = "[data-qa^='resume-editor-skills-recommended-']"
RESUME_SKILLS_CHIP = "[data-qa^='chips-trigger-chip-']"
RESUME_PARTIAL_EDIT_CANCEL = "[data-qa='resume-partial-edit-cancel']"
RESUME_PARTIAL_EDIT_SAVE = "[data-qa='resume-partial-edit-save']"

# Specializations in the position editor are a nested, multi-select tree.  The
# full tree (including the search and confirmation controls) was confirmed in
# the authenticated live DOM of the position editor on 2026-08-24 (issue
# #521).  The modal can remain open after selecting an option, so callers must
# wait for this dialog to hide after submitting it.
RESUME_SPECIALIZATION_ADD = "[data-qa='resume-position-professional-role-add-button']"
RESUME_SPECIALIZATION_MODAL = "[data-qa='professional-roles-modal']"
RESUME_SPECIALIZATION_SEARCH = "[data-qa='tree-selector-search-input']"
RESUME_SPECIALIZATION_OPTION = (
    "[data-qa^='tree-selector-item tree-selector-item-'][data-qa*='tree-selector-child-']"
)
RESUME_SPECIALIZATION_DELETE = "[data-qa='resume-position-professional-role-card-delete']"
RESUME_SPECIALIZATION_SUBMIT = "[data-qa='professional-roles-submit']"

# Language block and modal selectors confirmed on the authenticated read-only
# DOM of /applicant/profile/me on 2026-08-20 (issue #265).  Languages are a
# profile-level entity, not a resume-level one: /resume/{id} never renders a
# languages block (checked on an empty draft and on a published resume with
# real language data) — the card only exists on /applicant/profile/me and a
# saved language applies to every resume the account has, not to a single
# resume.  Each row is a <button data-qa="...-row-N"> with two nested
# [data-qa="cell-text"] elements: the first holds the language name, the
# second the level label (e.g. "Родной" or "C1 — Продвинутый") — read them by
# that structure, not by splitting the row's combined text.  The degree
# select shows only the six CEFR options (A1-C2); there is no guessed CEFR for
# an existing "Родной" (native) entry, so that value is out of scope for #265.
RESUME_LANGUAGE_CARD = "[data-qa='profile-language-card']"
RESUME_LANGUAGE_ROW = "[data-qa^='profile-language-card-row-']"
RESUME_LANGUAGE_ROW_CELL_TEXT = "[data-qa='cell-text']"
RESUME_LANGUAGE_ADD_BUTTON = "[data-qa='profile-language-add']"
RESUME_LANGUAGE_ADD_FORM = "[data-qa='profile-language-add-form']"
RESUME_LANGUAGE_FORM_LANGUAGE_SELECT = (
    "[data-qa='profile-language-add-form-language'] [data-qa='magritte-select-activator']"
)
RESUME_LANGUAGE_FORM_DEGREE_SELECT = (
    "[data-qa='profile-language-add-form-degree'] [data-qa='magritte-select-activator']"
)
RESUME_LANGUAGE_DEGREE_OPTION = "[data-qa='magritte-select-option-{}']"  # lowercase CEFR code
RESUME_LANGUAGE_SAVE = "[data-qa='profile-modal-button-save']"

# Live read-only research for issue #293 (2026-08-18).  These controls are
# rendered on the resume list card, but are kept here with the resume-page
# controls because they address one resume and the dialog is shared by both
# list/profile renderers.  The destructive confirm is deliberately distinct
# from the reversible "hide" action.
RESUME_DELETE_BUTTON = "[data-qa='resume-delete']"
RESUME_DELETE_TITLE = "[data-qa='resume-delete-title']"
RESUME_DELETE_CONTENT = "[data-qa='resume-delete-content']"
RESUME_DELETE_CONFIRM = "[data-qa='resume-delete-confirm']"
RESUME_DELETE_HIDE_CONFIRM = "[data-qa='resume-hide-confirm']"
RESUME_DELETE_CLOSE = "[data-qa='resume-delete-close']"

# Create-resume wizard (#304), confirmed against the authenticated live DOM
# on 2026-08-18.  The catalog is a live tree: top-level rows expand, while
# leaf professions have checkboxes.  Do not replace this with a hand-copied
# partial list; the tree search exposes the full current catalog.
RESUME_CREATE_BUTTON = "[data-qa='mainmenu_createResume']"
RESUME_CREATION_URL = "/profile/resume/professional_role"
RESUME_CREATION_SELECT_JOB = "[data-qa='resume-profile-card-select-job']"
RESUME_CREATION_POSITION = "[data-qa='resume-profile-position-input']"
RESUME_CREATION_NEXT = "[data-qa='resume-profile-next-screen']"
RESUME_CREATION_CATEGORY_SEARCH = "[data-qa='tree-selector-search-input']"
RESUME_CREATION_CATEGORY_SUBMIT = "[data-qa='category-modal-submit']"
RESUME_CREATION_CATEGORY_INPUT = "[data-qa~='tree-selector-input-{}']"

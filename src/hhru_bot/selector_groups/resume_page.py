"""Страница резюме (/resume/{hash}).

На живом DOM issue #225 кнопка публикации не появилась: свежая копия была
заблокирована незавершённым шагом ``professional_role``. Селекторы публикации
сохраняются как fail-closed кандидаты до отдельного подтверждения на готовом
к публикации черновике.
"""

from __future__ import annotations

from ._generated import optional_selector as _optional_selector
from ._generated import selector as _selector

# Existing bump selectors (confirmed by the bump feature's live check).
RESUME_BUMP_BUTTON = _selector("resume_page.RESUME_BUMP_BUTTON")
RESUME_BUMP_DISABLED_HINT = _selector("resume_page.RESUME_BUMP_DISABLED_HINT")

# Кандидаты из исходного флоу (#219); на заблокированной копии #225 ни один не
# присутствовал в живом DOM. Команда обязана проверять count и не угадывать.
RESUME_PUBLISH_BUTTON = "button:has-text('Опубликовать')"
RESUME_PUBLISH_BUTTON_DATA_QA = _optional_selector("resume_page.RESUME_PUBLISH_BUTTON_DATA_QA")
# Только read-only сообщение о текущей видимости; команда его не нажимает.
RESUME_VISIBILITY_BUTTON = "button:has-text('Изменить видимость')"

# Inline editor selectors confirmed by the authenticated read-only research in
# issue #268.  NOTE: #268 called this section inline (no /edit route), but #328
# found the same claim false for the neighbouring position/skills editors on
# this same profile page — this claim is unaudited against #328's finding and
# should not be trusted until re-verified on live DOM (see #328 follow-up).
RESUME_EDIT_ABOUT_BUTTON = _selector("resume_page.RESUME_EDIT_ABOUT_BUTTON")
RESUME_ABOUT_EDITOR = _selector("resume_page.RESUME_ABOUT_EDITOR")
RESUME_ABOUT_NO_EXPERIENCE_REASON = _selector("resume_page.RESUME_ABOUT_NO_EXPERIENCE_REASON")

# The profile-page control routes to /resume/edit/{id}/keySkills before the
# editor is mounted (authenticated live audit, #328).
RESUME_SKILLS_EDIT_BUTTON = _selector("resume_page.RESUME_SKILLS_EDIT_BUTTON")
RESUME_SKILLS_INPUT = _selector("resume_page.RESUME_SKILLS_INPUT")
RESUME_SKILLS_CHIP_INPUT = _selector("resume_page.RESUME_SKILLS_CHIP_INPUT")
RESUME_SKILLS_RECOMMENDED = _selector("resume_page.RESUME_SKILLS_RECOMMENDED")
# RESUME_SKILLS_CHIP (`chips-trigger-chip-*`) is the combobox widget's own chip
# markup, mounted only inside the keySkills editor. It is the right selector
# while the editor is open (dry-run cancel path, mid-edit reads), but it does
# not exist on the resume card the page returns to after save closes the
# editor (#813) — reading it there always observes zero chips regardless of
# what actually saved, a selector-scope bug rather than a render race.
RESUME_SKILLS_CHIP = _selector("resume_page.RESUME_SKILLS_CHIP")
# The published resume card renders each saved skill as a Magritte tag with
# `data-qa='skill-tag-{id}'`, grouped under `data-qa='skills-card'` — confirmed
# live 2026-08-30 (issue #813) via a Playwright DOM dump of a resume with
# saved skills (`skill-tag-1114` etc.), independent of the editor's own
# `chips-trigger-chip-*` markup above. This is the only selector that reflects
# what actually landed on hh.ru after the editor closes.
RESUME_SKILLS_DISPLAY_TAG = _selector("resume_page.RESUME_SKILLS_DISPLAY_TAG")
# #820: the resume card groups its skill tags under a level heading —
# `data-qa='skill-level-title-{n}'` — confirmed live 2026-08-30 (Playwright DOM
# dump, authenticated session, resume 24b16b4aff1106ca100039ed1f726766334230):
# `<div data-qa='skill-level-title-3'>Продвинутый уровень</div>` immediately
# followed by a sibling `<div>` wrapping that group's `skill-tag-*` tags — one
# heading per distinct level PLUS one heading with the fixed text
# "Уровень не указан" grouping every skill saved without a level. The heading's
# own parent element wraps both the title and its tags container, so
# `xpath=..` from the title, scoped to `[data-qa^='skill-tag-']` inside it,
# reads that group's tags without depending on the tags container's own
# (hashed, build-specific) CSS class. Used by read_display_skills to attach a
# level to each observed tag — #820: the pre-fix post-save Counter compared
# tag NAMES only, so a skill saved without a level because its
# _confirm_skill_levels radio was not found still reported [OK].
RESUME_SKILLS_LEVEL_TITLE = _selector("resume_page.RESUME_SKILLS_LEVEL_TITLE")
# The bare tag fragment (no `skills-card` scope prefix), reused to scope a
# search to one level group's own subtree via `title.locator(...)` — a
# locator method call is already scoped to its own element, so re-applying
# the `[data-qa='skills-card']` ancestor prefix from RESUME_SKILLS_DISPLAY_TAG
# would be redundant, not incorrect, but is kept as a separate constant here
# rather than derived from that selector by string-splitting it.
RESUME_SKILLS_TAG_IN_GROUP = _selector("resume_page.RESUME_SKILLS_TAG_IN_GROUP")
# Saving the keySkills editor with at least one skill that has no confirmed
# level (a brand-new skill, or an existing skill hh.ru could not carry a level
# for) does not return to the resume card directly — it navigates to a second
# wizard step, `/resume/edit/{id}/skillsLevels?fromBlock=keySkills`, with one
# radio group per pending skill (confirmed live 2026-08-30, issue #813). Each
# radio's `name` attribute is the skill name immediately followed by the
# Russian level label with no separator (e.g. `name="SeleniumСредний"`); the
# level cards render no `data-qa` of their own, and skill names can contain
# arbitrary characters, so the selector below is a raw `name=` attribute
# selector built by the caller per (skill, level), not a template constant
# here. This step reuses RESUME_PARTIAL_EDIT_SAVE for its own Save button.
# Skipping this step (as the pre-#813 code did) leaves the skill saved with no
# level ("Уровень не указан") and the editor stuck here, which is why the
# post-save chip/tag read observed zero: the skill never reached the resume
# card at all.
RESUME_SKILLS_LEVEL_RADIO_TEMPLATE = "input[name='{skill_and_level}']"
# #826: the keySkills combobox's Enter key never commits a chip, on an empty
# section or a non-empty one alike — confirmed live 2026-08-30 on both a
# resume with zero existing skills and one with six. Typing opens a
# `role='listbox'` autocomplete; the entered text is echoed back as its own
# `role='option'` item with `data-qa='suggest-item-user-input'` (distinct from
# the `resume-editor-skills-recommended-*` suggestion chips below the input,
# which match a *different*, pre-existing skill name and are not addressed
# here). Only clicking this option — never Enter — creates the chip.
RESUME_SKILLS_SUGGEST_USER_INPUT = _selector("resume_page.RESUME_SKILLS_SUGGEST_USER_INPUT")
RESUME_PARTIAL_EDIT_CANCEL = _selector("resume_page.RESUME_PARTIAL_EDIT_CANCEL")
RESUME_PARTIAL_EDIT_SAVE = _selector("resume_page.RESUME_PARTIAL_EDIT_SAVE")

# The Magritte select panel used by the position editor.  The panel remains
# mounted after selecting an option, so callers must close it explicitly and
# wait for this panel (rather than an option) to become hidden.
RESUME_POSITION_DROPDOWN = _selector("resume_page.RESUME_POSITION_DROPDOWN")

# Specializations in the position editor are a nested, multi-select tree.  The
# full tree (including the search and confirmation controls) was confirmed in
# the authenticated live DOM of the position editor on 2026-08-24 (issue
# #521).  The modal can remain open after selecting an option, so callers must
# wait for this dialog to hide after submitting it.
RESUME_SPECIALIZATION_ADD = _selector("resume_page.RESUME_SPECIALIZATION_ADD")
RESUME_SPECIALIZATION_MODAL = _selector("resume_page.RESUME_SPECIALIZATION_MODAL")
RESUME_SPECIALIZATION_SEARCH = _selector("resume_page.RESUME_SPECIALIZATION_SEARCH")
RESUME_SPECIALIZATION_OPTION = _selector("resume_page.RESUME_SPECIALIZATION_OPTION")
RESUME_SPECIALIZATION_DELETE = _selector("resume_page.RESUME_SPECIALIZATION_DELETE")
RESUME_SPECIALIZATION_SUBMIT = _selector("resume_page.RESUME_SPECIALIZATION_SUBMIT")

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
RESUME_LANGUAGE_CARD = _selector("resume_page.RESUME_LANGUAGE_CARD")
RESUME_LANGUAGE_ROW = _selector("resume_page.RESUME_LANGUAGE_ROW")
RESUME_LANGUAGE_ROW_CELL_TEXT = _selector("resume_page.RESUME_LANGUAGE_ROW_CELL_TEXT")
RESUME_LANGUAGE_ADD_BUTTON = _selector("resume_page.RESUME_LANGUAGE_ADD_BUTTON")
RESUME_LANGUAGE_ADD_FORM = _selector("resume_page.RESUME_LANGUAGE_ADD_FORM")
RESUME_LANGUAGE_FORM_LANGUAGE_SELECT = _selector("resume_page.RESUME_LANGUAGE_FORM_LANGUAGE_SELECT")
RESUME_LANGUAGE_FORM_DEGREE_SELECT = _selector("resume_page.RESUME_LANGUAGE_FORM_DEGREE_SELECT")
RESUME_LANGUAGE_DEGREE_OPTION = _selector("resume_page.RESUME_LANGUAGE_DEGREE_OPTION")
RESUME_LANGUAGE_SAVE = _selector("resume_page.RESUME_LANGUAGE_SAVE")

# Live read-only research for issues #293/#573 (2026-08-18, 2026-08-25).
# Drafts expose the delete control on the resume-list card; published resumes
# expose it only on the identity-confirmed resume page.  The confirmation dialog
# is shared by both renderers.  The destructive confirm is deliberately distinct
# from the reversible "hide" action.
RESUME_DELETE_BUTTON = _selector("resume_page.RESUME_DELETE_BUTTON")
RESUME_DELETE_TITLE = _selector("resume_page.RESUME_DELETE_TITLE")
RESUME_DELETE_CONTENT = _selector("resume_page.RESUME_DELETE_CONTENT")
RESUME_DELETE_CONFIRM = _selector("resume_page.RESUME_DELETE_CONFIRM")
RESUME_DELETE_HIDE_CONFIRM = _selector("resume_page.RESUME_DELETE_HIDE_CONFIRM")
RESUME_DELETE_CLOSE = _selector("resume_page.RESUME_DELETE_CLOSE")

# Create-resume wizard (#304), confirmed against the authenticated live DOM
# on 2026-08-18.  The wizard catalog (resume_wizard_roles screen family,
# #908 — not the vacancy-search catalog) is a live tree: top-level rows
# expand, while leaf professions have checkboxes.  Do not replace this with
# a hand-copied partial list; the tree search exposes the full current tree.
RESUME_CREATE_BUTTON = _selector("resume_page.RESUME_CREATE_BUTTON")
RESUME_CREATION_URL = "/profile/resume/professional_role"
RESUME_CREATION_SELECT_JOB = _selector("resume_page.RESUME_CREATION_SELECT_JOB")
RESUME_CREATION_POSITION = _selector("resume_page.RESUME_CREATION_POSITION")
RESUME_CREATION_POSITION_CLEAR = _selector("resume_page.RESUME_CREATION_POSITION_CLEAR")
RESUME_CREATION_NEXT = _selector("resume_page.RESUME_CREATION_NEXT")
RESUME_CREATION_CATEGORY_SEARCH = _selector("resume_page.RESUME_CREATION_CATEGORY_SEARCH")
RESUME_CREATION_CATEGORY_SUBMIT = _selector("resume_page.RESUME_CREATION_CATEGORY_SUBMIT")
RESUME_CREATION_CATEGORY_INPUT = _selector("resume_page.RESUME_CREATION_CATEGORY_INPUT")

# Second post-NEXT shape (#881, confirmed live DOM 2026-08-31 on a copy-resume
# draft): after the title step hh.ru skips the wizard catalog modal above entirely and
# instead renders a flat list of
# radio "chips" for popular professions, with the text entered on the previous
# step highlighted as a chip (checked). Copies start with no professional role;
# the text is not retained in the input and has no role_id. The saver must
# check for this shape before treating
# an absent catalog search input as a failure — see the two-shape branch there.
RESUME_CREATION_POSITION_CHIP_POPULAR = _selector(
    "resume_page.RESUME_CREATION_POSITION_CHIP_POPULAR"
)

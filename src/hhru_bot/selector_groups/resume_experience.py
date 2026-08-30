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

**#815 live write test (2026-08-30): this route is NOT a general "add a new
row" mechanism on a resume that already has experience entries.** Live
testing on a draft with 3 existing rows found it opened blank once, and on a
separate attempt opened pre-filled with — and, on save, overwrote — an
UNRELATED existing row (matched by some identity other than the row clicked,
observed matching on start_year/start_month). It is used in ``experience.py``
only for the one shape confirmed safe: a resume with genuinely zero
experience rows. ``edit_experience_on_hh`` fails closed rather than falling
back to this route when asked to address a row index that does not exist on
a resume that already has other rows.

**A THIRD shape exists (#840, read-only + JS-confirmed 2026-08-30) for "add
a new row on a resume that already has entries" — the case #815 above fails
closed on.** Clicking ``EXPERIENCE_ADD_BUTTON`` on such a resume lands on
``/profile/edit/experience?resumeFrom={resume_id}``: the same shared-profile
URL as the unreliable suggestion-chip route above, but reached via a
different in-page control and, unlike that chip, WITH a ``resumeFrom``
binding that was confirmed correct (its "Резюме с этим местом работы"
checkbox panel showed only this account's resumes, this one pre-checked).
The form opened genuinely blank (no unrelated row silently loaded, unlike
#815's finding for the other route) and uses a third data-qa namespace for
its save/cancel controls (``SHARED_EXPERIENCE_SAVE``/``SHARED_EXPERIENCE_CANCEL``,
``profile-layout-*-button``) while reusing the month-combobox constants
unchanged for its other fields. Its company/position fields use the SAME
``resume-profile-experience-specific-{company,position}-input-{index}``
data-qa pattern as the indexed row editor above, but the ``{index}`` is a
fresh React counter unrelated to any requested row index (#840 observed
``24`` on a form opened for a brand-new row) — ``edit_experience_on_hh``
therefore cannot format ``EXPERIENCE_COMPANY``/``EXPERIENCE_POSITION`` with
its own loop index for this shape; it uses the prefix-scoped
``EXPERIENCE_SHARED_NEW_ROW_COMPANY``/``EXPERIENCE_SHARED_NEW_ROW_POSITION``
below instead and requires exactly one match, the same fail-closed pattern
as ``EXPERIENCE_EDIT_BUTTONS_ALL``.

**#782/#787: this shape's "Резюме с этим местом работы" binding panel
defaults to pre-checking EVERY resume in the account for a brand-new row**
(not just the ``resumeFrom`` one, live-confirmed on 7 resumes) — saving
without reconciling it silently binds the new row to every resume, published
ones included. ``edit_experience_on_hh`` now wires this shape in for the
"add a new row to a non-empty resume" case and always reconciles the panel
(via ``_reconcile_experience_resume_panel`` in ``experience.py``) before
clicking save — see ``EXPERIENCE_RESUME_PANEL_*`` below.
"""

from __future__ import annotations

from ._generated import selector as _selector

EXPERIENCE_EDIT_BUTTON = _selector("resume_experience.EXPERIENCE_EDIT_BUTTON")
# #815/#833: EXPERIENCE_EDIT_BUTTON's {index} is an internal React counter
# shared across the whole resume page (edit/add controls of every editable
# block), not the row's position — confirmed live 2026-08-30: indices were
# sparse and never started at 0 (observed sets 2,3,4 on one resume and
# 1,6,7,8,12,17 on another), with no relation to row count or on-page order.
# `_experience_row_indexes()` must therefore enumerate the actually-present
# buttons rather than probe range(0, N) and stop at the first gap — that
# undercounts (or returns 0) whenever index 0 happens to be free. The
# trailing `-svg` sibling shares the same data-qa prefix (icon inside the
# button) and must be excluded or it would double-count every row. The set
# was also confirmed STABLE across an open/cancel cycle on the same page
# (open one row's form, cancel it — the same full set comes back), so a
# snapshot taken once is safe to reuse for the rest of a read/edit pass.
EXPERIENCE_EDIT_BUTTONS_ALL = _selector("resume_experience.EXPERIENCE_EDIT_BUTTONS_ALL")
# #815: hh.ru collapses the experience list to 3 visible cards (and their
# edit buttons) behind a "Развернуть" control once a resume has more than 3
# entries — confirmed live 2026-08-30 (5-entry draft: 3 buttons collapsed,
# 5 after this control is clicked). No data-qa of its own; scoped by text
# into the same resume-list-card-experience container as
# EXPERIENCE_ADD_BUTTON. Confirmed absent (count=0) on resumes with 3 or
# fewer entries, including empty drafts — `_experience_row_indexes()` must
# expand before counting or it silently undercounts any resume past the
# collapse threshold.
EXPERIENCE_EXPAND_BUTTON = _selector("resume_experience.EXPERIENCE_EXPAND_BUTTON")
EXPERIENCE_COMPANY = _selector("resume_experience.EXPERIENCE_COMPANY")
EXPERIENCE_POSITION = _selector("resume_experience.EXPERIENCE_POSITION")
EXPERIENCE_COMPANY_URL = _selector("resume_experience.EXPERIENCE_COMPANY_URL")
EXPERIENCE_START_YEAR = _selector("resume_experience.EXPERIENCE_START_YEAR")
EXPERIENCE_END_YEAR = _selector("resume_experience.EXPERIENCE_END_YEAR")
# #811: month comboboxes on the same first-row/full-page shape (confirmed
# live 2026-08-30 on a draft resume, /resume/edit/{resume_id}/experience).
# Both are magritte `role="combobox"` triggers; clicking one opens a
# `role="listbox"` popup with 12 `role="option"` items whose data-qa is
# `magritte-select-option-{01..12}` (confirmed live, see EXPERIENCE_MONTH_OPTION
# below). start-month has no confirmed default; end-month starts disabled
# (its "Работаю сейчас" checkbox is checked by default, same shape as
# EXPERIENCE_END_YEAR/#800) — is_enabled() must be checked the same way.
EXPERIENCE_START_MONTH = _selector("resume_experience.EXPERIENCE_START_MONTH")
EXPERIENCE_END_MONTH = _selector("resume_experience.EXPERIENCE_END_MONTH")
# Month option inside the opened listbox popup, addressed directly by its
# 2-digit month number (01..12) — same identity-bound pattern as
# apply_form.APPLY_RESUME_OPTION (data-qa already carries the value, no
# separate label/index lookup needed). Confirmed live 2026-08-30: exactly 12
# `role="option"` items, `magritte-select-option-01`=Январь .. `-12`=Декабрь.
EXPERIENCE_MONTH_OPTION = _selector("resume_experience.EXPERIENCE_MONTH_OPTION")
# The popup itself, used to confirm it closed after picking an option (same
# role as apply_form.APPLY_RESUME_DROPDOWN) — `role="listbox"` count drops to
# 0 once a month is selected (confirmed live 2026-08-30).
EXPERIENCE_MONTH_LISTBOX = _selector("resume_experience.EXPERIENCE_MONTH_LISTBOX")
EXPERIENCE_DESCRIPTION = _selector("resume_experience.EXPERIENCE_DESCRIPTION")
EXPERIENCE_CANCEL = _selector("resume_experience.EXPERIENCE_CANCEL")
EXPERIENCE_SAVE = _selector("resume_experience.EXPERIENCE_SAVE")

# The historical candidate ``resume-profile-experience-add-button`` does not
# exist on hh.ru (live probe 2026-08-29 and 2026-08-30, count=0 on resumes with
# and without experience).  The real "Добавить" control is scoped inside the
# experience card and carries the shared ``data-qa='link'`` — the same shape as
# ``resume_education.PRIMARY_ADD``/``ADDITIONAL_ADD``.  #782/#840: confirmed
# live and wired into ``edit_experience_on_hh`` as the third (shared-profile-
# editor) shape's entry point for adding a row to a resume that already has
# experience — every caller must still require one unambiguous match, because
# the generic ``link`` value is only meaningful together with the card scope.
EXPERIENCE_ADD_BUTTON = _selector("resume_experience.EXPERIENCE_ADD_BUTTON")

# First-row editor at /resume/edit/{resume_id}/experience (#786/#787, live
# write-confirmed 2026-08-25/29 on a resume with zero experience entries).
FIRST_EXPERIENCE_COMPANY = _selector("resume_experience.FIRST_EXPERIENCE_COMPANY")
FIRST_EXPERIENCE_POSITION = _selector("resume_experience.FIRST_EXPERIENCE_POSITION")
FIRST_EXPERIENCE_SAVE = _selector("resume_experience.FIRST_EXPERIENCE_SAVE")
FIRST_EXPERIENCE_CANCEL = _selector("resume_experience.FIRST_EXPERIENCE_CANCEL")

# #800/#824: "Работаю сейчас" checkbox above the end-date fields (confirmed
# live 2026-08-30 on /resume/edit/{resume_id}/experience, draft resume).
# Checked by default on a new entry — while checked, the end-year/end-month
# controls are disabled. This is a Magritte custom-checkbox triple: a bare
# physical <input type="checkbox"> (no data-qa of its own) plus a purely
# visual <span data-qa="checkbox"> sibling that paints the glyph, both inside
# <span data-qa="checkbox-container">. The visual span sits in front of the
# input in DOM order, so a click on `[data-qa='checkbox']` (the pre-#824
# value) is intercepted by the physical <input> underneath — the exact
# Locator.click timeout in #824's call log. Scoping into the container and
# targeting its `input` descendant clicks the real control; both
# `[data-qa='checkbox-container']` and its `input` descendant were confirmed
# to resolve to exactly one match on this form (count()==1), and clicking it
# was live write-confirmed to toggle the checkbox and re-enable the end-date
# fields (form not saved, no mutation reached hh.ru).
FIRST_EXPERIENCE_CURRENT_CHECKBOX = _selector("resume_experience.FIRST_EXPERIENCE_CURRENT_CHECKBOX")

# Third DOM shape (#840, read-only + JS-confirmed 2026-08-30): clicking
# EXPERIENCE_ADD_BUTTON ("Добавить" inside the experience card) on a resume
# that ALREADY has experience entries lands on
# /profile/edit/experience?resumeFrom={resume_id} — the shared-profile
# editor, not /resume/edit/{resume_id}/experience (that route is confirmed
# safe only for the zero-rows case, see FIRST_EXPERIENCE_* above and #815).
# This shape's save/cancel controls use a THIRD data-qa namespace
# (profile-layout-*-button), distinct from both EXPERIENCE_SAVE/CANCEL
# (indexed row editor) and FIRST_EXPERIENCE_SAVE/CANCEL (resume-partial-edit-*).
# Company/position fields on this shape were confirmed to reuse the exact
# same indexed data-qa pattern as EXPERIENCE_COMPANY/EXPERIENCE_POSITION
# (resume-profile-experience-specific-{company,position}-input-{index},
# observed index=24 — a fresh React counter unrelated to any existing row
# index) — no new constant needed for those, or for the month comboboxes
# (see below).
SHARED_EXPERIENCE_SAVE = _selector("resume_experience.SHARED_EXPERIENCE_SAVE")
SHARED_EXPERIENCE_CANCEL = _selector("resume_experience.SHARED_EXPERIENCE_CANCEL")

# #782: company/position for a row created on THIS shape via
# EXPERIENCE_ADD_BUTTON — same data-qa pattern as EXPERIENCE_COMPANY/
# EXPERIENCE_POSITION but prefix-scoped (no {index} substitution), because
# the row's index is a fresh React counter the caller cannot predict ahead
# of opening the form (see module docstring). A freshly opened add-form has
# exactly one such field; callers must still require count()==1 rather than
# assume it, the same fail-closed pattern as EXPERIENCE_EDIT_BUTTONS_ALL's
# prefix scoping above.
EXPERIENCE_SHARED_NEW_ROW_COMPANY = _selector("resume_experience.EXPERIENCE_SHARED_NEW_ROW_COMPANY")
EXPERIENCE_SHARED_NEW_ROW_POSITION = _selector(
    "resume_experience.EXPERIENCE_SHARED_NEW_ROW_POSITION"
)

# #840 blocker investigation: the issue's first-candidate hypothesis was a
# #824-style visual-element interception (a decorative span sitting in front
# of the real control in DOM order, absorbing the click). CONFIRMED FALSE on
# this shape: document.elementFromPoint() at the activator's bounding-box
# center returned the [data-qa='magritte-select-activator'] element itself,
# not an overlapping sibling — there is no interception here, the DOM around
# the month combobox is structurally IDENTICAL to the already-working
# EXPERIENCE_START_MONTH/END_MONTH triggers (same magritte-trigger markup, no
# extra wrapper). A plain JS `.click()` on the activator opened the listbox
# normally (aria-expanded flipped false -> true), and EXPERIENCE_MONTH_OPTION
# / EXPERIENCE_MONTH_LISTBOX matched the resulting popup unchanged
# (magritte-select-option-{01..12} / role=listbox with data-qa='drop-base'),
# confirming `_select_month()` (experience.py) needs no shape-specific
# variant for this control. The originally reported non-opening Playwright
# click is more likely explained by a click landing before this specific
# screen finished hydrating after the EXPERIENCE_ADD_BUTTON navigation (the
# general "commit is not painted" pattern already documented in CLAUDE.md) —
# not confirmed by a live write test in this issue's scope, left for the
# follow-up that wires this shape into edit_experience_on_hh to verify with
# an explicit wait_for(state="visible") before the first click, the same
# pattern already used by _select_month's own caller.
#
# Also note for that follow-up: FIRST_EXPERIENCE_CURRENT_CHECKBOX's
# `[data-qa='checkbox-container'] input` scoping is NOT reusable as-is on
# this shape — the form has three checkbox-container elements ("Работаю
# сейчас" plus one per resume in the "Резюме с этим местом работы" list), so
# count() is 3 here vs. 1 on the first-entry shape; a narrower scope will be
# needed before this control can be driven safely.

# #782/#787 live read-only recon (2026-08-30): the "Резюме с этим местом
# работы" checkbox panel on the shared-editor shape (both EXPERIENCE_ADD_BUTTON
# for a NEW row and EXPERIENCE_EDIT_BUTTON for an EXISTING one navigate here —
# same compose screen). Confirmed structure:
#   <h3>Резюме с этим местом работы</h3>
#   <label data-qa="cell"><span data-qa="checkbox-container">
#     <input type="checkbox" aria-label="{resume title}">
#     <span data-qa="checkbox"></span></span>...</label>...
#   <button>Развернуть<span>ещё N</span></button>  (only when the account has
#     more than 2 resumes — collapsed by default, remaining checkboxes are not
#     in the DOM at all until this is clicked, same collapse pattern as
#     EXPERIENCE_EXPAND_BUTTON above but a DIFFERENT control/container).
# The <input> carries no data-qa of its own; aria-label + the label[data-qa=
# 'cell'] scope is what makes it addressable. The panel itself has no data-qa
# either, so it is scoped by its unique <h3> text via xpath (same
# ancestor-lookup pattern already used by apply/questions.py's
# `xpath=ancestor::form[1]` form-scoping) rather than guessing a
# class-based container. **Confirmed by default checkbox state, NOT
# clicked/saved**: on a NEW row (via EXPERIENCE_ADD_BUTTON) every resume in
# the account is pre-checked, including the target one — saving unmodified
# silently binds the new entry to ALL resumes, not just the target
# (#782 "silent over-binding" finding). On an EXISTING row (via
# EXPERIENCE_EDIT_BUTTON) the panel instead reflects that row's actual
# current binding (some checked, some not) — the caller must not blindly
# uncheck non-target resumes there, only ensure the target one is checked.
#
# #782 PR review: the individual checkbox is deliberately NOT a
# _selector()-registered CSS string here. A resume title is untrusted free
# text a user can name however they like (an apostrophe is a plausible
# Russian title, e.g. "Data Engineer's..."), and interpolating it into a
# hand-built `input[aria-label='{title}']` CSS attribute selector breaks the
# selector's quoting for every OTHER title looked up in the same pass, not
# just the offending one — experience.py's
# ``_reconcile_experience_resume_panel`` instead resolves each checkbox via
# ``scope.get_by_role("checkbox", name=title, exact=True)``, the same
# accessible-name-matching pattern already used for other free-text labels
# in this codebase (professional_roles.py, resume_position.py) — Playwright
# handles the string safely internally, no manual CSS construction at all.
EXPERIENCE_RESUME_PANEL_SCOPE = _selector("resume_experience.EXPERIENCE_RESUME_PANEL_SCOPE")
EXPERIENCE_RESUME_PANEL_EXPAND = _selector("resume_experience.EXPERIENCE_RESUME_PANEL_EXPAND")

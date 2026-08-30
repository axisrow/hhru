# Issue #265: authenticated CEFR/languages probe

Date: 2026-08-20 (updated same day after the route was found)

## Scope and safety

The probe used the authenticated storage state and only read-only navigation,
DOM inspection, and opening/cancelling the "Добавить" language dialog (no
save was clicked; a "Сохранить изменения?" confirm was dismissed with "Не
надо" each time a dialog was closed with unsaved input). No language was
added, edited, or removed on the live account.

## Earlier attempt: wrong page, not a missing feature

The first probe pass (recorded below as history) checked only `/resume/{id}`
routes and the `/profile/resume` creation wizard, and concluded no
language/CEFR DOM existed anywhere. That conclusion was wrong: it never
checked `/applicant/profile/me`.

| Route | Observed result |
| --- | --- |
| `/resume/<id>` | Authenticated resume view. **Confirmed on both an empty draft and a published resume with real language data**: no languages card, no `[data-qa*="language"]` element, no `Знание языков`/`Языки` text anywhere on the page. Languages are not a resume-page section at all. |
| `/resume/<id>/edit`, `/resume/edit/<id>`, `/resume/edit/<id>/languages` | Authenticated 404. |
| `/applicant/resumes/<id>/edit`, `/applicant/resumes/<id>/edit/languages` | JSON shell, no rendered form DOM. |
| `/profile/resume?resume=<id>` | Redirects to `/profile/resume/professional_role`; wizard is unrelated to editing an existing resume's languages. |
| **`/applicant/profile/me`** | **Renders the "Языки" (Languages) card with existing entries and an "+ Добавить" control.** This is the actual route. |

## Confirmed: languages are a profile-level entity, not a resume-level one

A saved language is shared across every resume on the account — there is no
per-resume language list. `edit_languages_on_hh()` navigates to
`/applicant/profile/me`; `--resume` only selects which account/session to use,
not which resume the write targets. The CLI's write-confirmation prompt says
this explicitly before any WRITE.

## Confirmed DOM (read-only, `/applicant/profile/me`)

- Languages card: `[data-qa="profile-language-card"]`.
- Each existing entry is a `<button data-qa="profile-language-card-row-N">`
  with two nested `[data-qa="cell-text"]` children — first is the language
  name, second is the level label (e.g. `"Родной"` or `"C1 — Продвинутый"`).
  The row's raw `textContent` has **no separator** between them (e.g.
  `"РусскийРодной"`); read the two `cell-text` children separately, do not
  split on `,`.
- Add control: `[data-qa="profile-language-add"]`.
- Clicking it opens `[role="dialog" data-qa="profile-languages-editor-modal"]`
  (accessible name "Язык", confirmed via `aria-labelledby` pointing at an
  `<h4 data-qa="title">Язык</h4>`) containing
  `[data-qa="profile-language-add-form"]`, with two select activators inside:
  - `[data-qa="profile-language-add-form-language"] [data-qa="magritte-select-activator"]`
  - `[data-qa="profile-language-add-form-degree"] [data-qa="magritte-select-activator"]`
- **Both the language list and the CEFR-level list open inside the same
  single dialog** (there is one `role="dialog"` on the page at a time, named
  "Язык") — not two separately-named dialogs. A prior implementation attempt
  assumed a second dialog named "Уровень владения" for the degree picker;
  that dialog does not exist and the lookup would always fail.
- Degree options carry stable, code-based `data-qa`:
  `magritte-select-option-a1` … `magritte-select-option-c2` (lowercase CEFR
  code). Exactly six options, all pure CEFR — confirmed by reading every
  `[role="option"]` in the open degree picker. There is no CEFR value for an
  existing "Родной" (native) entry; that value is out of scope for #265 (LLM
  output and manual `--language` input are restricted to `CEFR_LEVELS`).
- Save/cancel/close: `[data-qa="profile-modal-button-save"]`,
  `[data-qa="profile-modal-button-cancel"]`,
  `[data-qa="profile-modal-button-close"]`.
- CEFR labels observed on the six live options (`A1 — Начальный`,
  `A2 — Элементарный`, `B1 — Средний`, `B2 — Средне-продвинутый`,
  `C1 — Продвинутый`, `C2 — В совершенстве`). The implementation keys on the
  option `data-qa` codes (`magritte-select-option-{code}`), not on these
  label strings, so no `CEFR_LABELS` constant is wired into `languages.py`
  (code-review round 2 removed the unused one that had been added earlier).

## Other allowed drafts (kept for history — irrelevant to the fix)

- `11112222333344445555666677778888999900`: same `professional_role` wizard
  state and no languages block on the resume view (expected: that view was
  never the right page).
- `22223333444455556666777788889999000011`: profile wizard reports
  `Problem fetching content` (unrelated to languages).

## Outcome

Selectors and page for the language add flow are confirmed against live DOM.
`languages.py`/`edit_languages.py`/`selector_groups/resume_page.py` were
updated to use `/applicant/profile/me`, the `cell-text`-based existing-entry
parser, and the code-based degree option selector instead of the earlier
(unconfirmed, and in one case non-existent) selectors.

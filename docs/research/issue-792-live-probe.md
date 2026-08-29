# Issue #792: live hh.ru resume title probe

Read-only observation in Chrome on 2026-08-29. No login, form submission, or
other write action was performed.

## Symptom

`competitors collect` saved 0 resumes on every query tried (`--text GPT`,
`--text Cursor`, `--text Codex`), always failing with
`CompetitorResumeIndeterminate: desired_role не подтверждён`.

## Public resume detail

On a public `/resume/{id}` page, the desired-role title is not an `h2` at
all — it is rendered as the page's `h1`:

```html
<h1 data-qa="bloko-header-2" class="bloko-header-2">
  <span class="resume-block__title-text" data-qa="resume-block-title-position">
    <span><span>GPT</span></span>
  </span>
</h1>
```

`Array.from(document.querySelectorAll('main h2')).map(h => h.innerText)`
returned only the standard section headings and the salary line, e.g. for
resume `05d5fdfd00061844b70039ed1f544d336f4661` ("GPT"):

```text
["90 000 ₽ на руки", "Опыт работы 3 года 2 месяца", "Навыки",
 "Опыт вождения", "Образование", "Знание языков",
 "Гражданство, время в пути до работы"]
```

None of these `h2` values is the desired role — every one of them is either
the salary heading or a standard section heading that
`parse_competitor_resume_text` already filters out via `_SECTION_HEADINGS` /
`_EXPERIENCE_PREFIXES` / `is_salary_heading`. With the title absent from
`main h2` entirely, `desired_candidates` is always empty, so every detail
fetch raises `CompetitorResumeIndeterminate`.

Confirmed on a second resume (`8fae16df00013ba1190039ed1f4b5458503138`,
title "Специалист повышения качества GPT и ML"): same shape — the title is
`h1[data-qa="resume-block-title-position"]`, absent from `main h2`.

## Fix

Read the desired role directly via
`h1 [data-qa='resume-block-title-position']` instead of trying to recover it
from the `main h2` heading list. `main h2` remains the source for salary,
experience duration, and section headings — no discrepancy found there.

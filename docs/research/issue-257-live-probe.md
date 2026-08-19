# Issue #257: live response-form probe

The authenticated read-only probe dumps below confirm the response-form
question markup on hh.ru. The dumps are intentionally kept under
`data/logs/` (ignored by git) because they contain private candidate and
vacancy data.

## Confirmed dumps

| Vacancy | Dump | Captured |
|---|---|---|
| `133801099` | `data/logs/probe_133801099_form.html` | 2026-07-27 |
| `136230351` | `data/logs/probe_136230351_questions_live.html` | 2026-08-16 |

Both dumps contain the response form:

```css
form[name="vacancy_response"]
```

Within that form, the question container and question text are:

```css
form[name="vacancy_response"] [data-qa="task-body"]
form[name="vacancy_response"] [data-qa="task-question"]
```

### Vacancy `133801099`

This dump contains both radio and free-text questions:

```css
form[name="vacancy_response"] input[type="radio"]
form[name="vacancy_response"] textarea
```

The two radio groups use names `task_360778388` and `task_360778391`; the
three free-text controls use names `task_360778394_text`,
`task_360778395_text`, and `task_360778396_text`.

Observed question text:

- `У тебя есть опыт работы с Python от 4х лет?` — radio
- `Работал ли ты с многопользовательскими проектами/высокой нагрузкой?` — radio
- `От какой суммы сейчас отталкиваешься (на руки)?` — textarea
- `Территориально где находишься на данный момент?` — textarea
- `Готов подтвердить свой опыт работы выпиской из госулуг о трудовой деятельности?` — textarea

### Vacancy `136230351`

This dump contains three additional free-text questions, also under
`[data-qa="task-body"]`, including the PostgreSQL experience question. It
confirms that question controls do not need their own `data-qa` attribute;
their `name` begins with `task_` and the question text is in
`[data-qa="task-question"]`.

## Reproduction

With a valid local hh.ru session:

```sh
hhru --headless probe --resume python --vacancy-id 133801099
```

The command is read-only: it navigates to the vacancy and response form and
never clicks the submit control. The HTML dump is a diagnostic artifact and
must not be committed.

## Representative live sample (2026-08-20)

The selector evidence was expanded to ten distinct vacancy IDs. All ten
contained a rendered `form[name="vacancy_response"]` and at least one
`[data-qa="task-body"]`; the counts below are from the question-bearing
controls in each saved form dump.

| Vacancy | URL | `task-body` | Radio | Checkbox | Textarea | Dump |
|---|---|---:|---:|---:|---:|---|
| `133801099` | `https://hh.ru/vacancy/133801099` | 5 | 4 | 0 | 3 | `probe_133801099_form.html` |
| `136230351` | `https://hh.ru/vacancy/136230351` | 3 | 0 | 0 | 3 | `probe_136230351_questions_live.html` |
| `130637097` | `https://hh.ru/vacancy/130637097` | 10 | 6 | 0 | 7 | `probe_130637097_form_initial.html` |
| `136098899` | `https://hh.ru/vacancy/136098899` | 6 | 2 | 0 | 6 | `probe_136098899_form_initial.html` |
| `136230349` | `https://hh.ru/vacancy/136230349` | 3 | 0 | 0 | 3 | `probe_136230349_form_initial.html` |
| `136230350` | `https://hh.ru/vacancy/136230350` | 3 | 0 | 0 | 3 | `probe_136230350_form_initial.html` |
| `136348378` | `https://hh.ru/vacancy/136348378` | 11 | 11 | 25 | 3 | `probe_136348378_form_initial.html` |
| `136401145` | `https://hh.ru/vacancy/136401145` | 7 | 0 | 0 | 7 | `probe_136401145_form_initial.html` |
| `135397152` | `https://hh.ru/vacancy/135397152` | 5 | 0 | 20 | 0 | `probe_135397152_form_initial.html` |
| `135046068` | `https://hh.ru/vacancy/135046068` | 3 | 0 | 4 | 3 | `probe_135046068_form_initial.html` |

The sample confirms all requested control families: radio-only and
radio-plus-text forms, checkbox questionnaires (including multi-select
groups), textarea-only questionnaires, and mixed checkbox-plus-text forms.
The repeated PostgreSQL questionnaire on vacancies `136230349`–`136230351`
is retained as three separate live vacancy examples, but the mixed-control
cases provide the broader selector coverage.

The live run initially stopped before some forms because
`_hidden_resume_warning_is_expanded()` passed the selector positionally to
Playwright Python. That call now uses `arg=selector`; a real Chromium
regression test covers the same JavaScript visibility check in
`tests/test_apply_steps.py::test_hidden_resume_warning_uses_playwright_named_arg`.

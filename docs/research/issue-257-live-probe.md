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

## Probe follow-up (2026-08-20)

The live run initially stopped before the form on every new candidate because
`_hidden_resume_warning_is_expanded()` passed the selector positionally to
Playwright Python. That call now uses `arg=selector`; a real Chromium
regression test covers the same JavaScript visibility check in
`tests/test_apply_steps.py::test_hidden_resume_warning_uses_playwright_named_arg`.

The first follow-up candidate (`136190066`) reached a clean response form but
had no `task-body` questions. Subsequent candidates were stopped by the
authenticated session/browser challenge before a reliable form dump could be
captured. Therefore this document deliberately still reports **two** confirmed
question-bearing vacancies, not ten; no unobserved combinations are inferred.

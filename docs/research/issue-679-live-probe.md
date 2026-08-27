# Issue #679: live hh.ru geography probe

Read-only observation in Chrome on 2026-08-27. No login, form submission, or
other write action was performed.

## Search results

On `https://hh.ru/search/resume`, each observed resume card used:

- card: `[data-qa='resume-serp__resume']`;
- title link: `[data-qa='serp-item__title']`;
- combined geography/travel field:
  `[data-qa='resume-serp_resume-item-area-and-relocation-content']`.

The combined field rendered examples such as `Екатеринбург • Не готов к
командировкам`, `Москва • Готова к командировкам`, and
`Подольск (Московская область) • Не готов к командировкам`. The area is the
text before `•`; the business-trip readiness is the second part. Thus the
geography is already present in the result card and does not require a detail
page solely for geography.

No observed result card contained a metro station marker.

## Public resume detail

On a public `/resume/{id}` page, the header rendered a paragraph like:

```html
<p><span data-qa="resume-personal-address">Екатеринбург</span>,
<span data-qa="relocation_no_relocation">не готов к переезду</span>,
не готов к командировкам</p>
```

The city is therefore the optional
`[data-qa='resume-personal-address']` element. Relocation has a dedicated
`relocation_*` marker; in the observed sample its exact value was
`relocation_no_relocation`. Business-trip readiness was plain text in the
same paragraph, not a separate data-qa field. The observed detail page did
not contain a metro station.

If the city is absent, the address marker/card area is absent or empty. The
collector treats that as `NULL`, not as a guessed city or an empty string.

## Live READ smoke

The exact command from the issue was first run and correctly rejected because
the CLI requires `--text` (exit 2). The valid headless anonymous READ smoke
used the same config and no login/write action:

```text
./scripts/run.sh --config /Users/axisrow/Projects/hhru/data/config.yaml --headless competitors collect --auth-mode anonymous --text AI --max-pages 1 --items-per-page 20 --detail-workers 10
```

Result: exit 0; 1 page, 20 cards, 20 unique snapshots saved, 0 errors. Full
output is in `artifacts/issue-679-live-read.log`.

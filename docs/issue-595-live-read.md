# Issue 595 live READ evidence

Run: 2026-08-25, saved authenticated session, `search --dry-run --text python --max-pages 1`; no WRITE. Card text is the stored search-card `vacancy_text`; body is `[data-qa='vacancy-description']` on the exact vacancy URL. Full body was read in batches to avoid timeout.

| vacancy | card chars | full body chars | valid |
|---|---:|---:|:---:|
| 134991226 | 508 | 2232 | yes |
| 136574613 | 468 | 1843 | yes |
| 136598407 | 408 | 2183 | yes |
| 136597987 | 464 | 2714 | yes |
| 135648953 | 477 | 1833 | yes |
| 133150728 | 455 | 2693 | yes |
| 135720555 | 418 | 812 | yes |
| 136608786 | 478 | 3317 | yes |
| 136608583 | 288 | 1112 | yes |
| 136607953 | 510 | 1482 | yes |

The full body is 1.9x–6.9x the card text (median approximately 4.5x), so the model receives materially more context than the search card. This is a READ-only comparison; no scoring/letter submit was performed.

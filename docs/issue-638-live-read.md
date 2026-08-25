# Issue #638 live calibration

Commands were run on 2026-08-25 with the real saved session and
`/Users/axisrow/Projects/hhru/data/config.yaml`:

```text
./scripts/run.sh --config ... search --dry-run --max-pages 3
./scripts/run.sh --config ... apply --resume marketing --dry-run --max-pages 3 --limit 20
```

## letter_match_score

The apply dry-runs produced 40 observations (two completed dry-run batches).

```text
min 13.3   p10 18.8   median 27.8   p90 40.9   max 45.8
10-19: 8   20-29: 17   30-39: 7   40-49: 8
```

The observed values are far below 90.0; therefore 90.0 would reject all 40
observed letters. The threshold remains disabled by default and is opt-in;
this sample does not justify enabling a production threshold automatically.

## resume_match_score

The search collected more than 299 cards, but produced **zero**
`resume-match ... /100` observations. The live config has no `ai_profile`, and
`_log_resume_match(profile=None)` intentionally has no profile to score. The
implementation now emits a warning instead of silently hiding this condition:

```text
resume_match пропущен: ai_profile не сконфигурирован
```

Consequently no live resume distribution or calibrated resume threshold is
claimed here. The resume threshold is implemented as an opt-in config value
and remains `None` by default until a configured `ai_profile` is measured.

Raw command output is preserved in `issue-638-search-live.log` and
`issue-638-apply-live.log`.

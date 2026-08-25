# Selector provenance contract

`reference-map.yaml` is the machine-readable source of truth. Every managed
`data-qa` selector has a logical id, READ/WRITE criticality, exact occurrences
in the three approved reference projects, and sanitized local live-DOM
evidence where available. `reference-matrix.md` is the generated review view;
`src/hhru_bot/selector_groups/_generated.py` is the generated runtime view.

CI rejects:

- any runtime `data-qa` literal outside the generated selector contract;
- a generated file that differs from the map;
- false 2-of-3 consensus or incomplete evidence;
- unresolved upstream drift.

The daily GitHub Action reads, but never executes, the three reference
checkouts. It follows the stored source keys across upstream commits. In the
current `manual` phase, a changed selector produces a review PR and cannot
merge while drift is unresolved. `--mode read_auto` is reserved for a later
phase: it may update only READ selectors with new 2-of-3 consensus. WRITE
selectors always remain manual. Criticality is based on reachability: a title,
status, or container is WRITE-critical when a mutation uses it for identity,
scoping, or post-save verification, even if that element itself is only read.

Local commands:

```bash
python scripts/selector_contracts.py check
python scripts/selector_contracts.py render
python scripts/selector_contracts.py refresh --dry-run --mode manual --reference-root /path/to/references
python scripts/selector_contracts.py refresh --mode manual --reference-root /path/to/references
```

The authoritative runtime check is the authenticated read-only live healthcheck,
not an upstream reference snapshot. Run it from the checkout with
`PYTHONPATH=src` and `probe --healthcheck --json`; the JSON section is derived
directly from the live `locator.count()` observations. Reference drift and the
Issue 599 `10 + 3 - 1 = 12` scope remain characterization/reporting data and
must not turn a live `NOT_FOUND` into an `OK` result.

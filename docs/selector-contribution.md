# Selector contribution guide

`selectors/reference-map.yaml` is the canonical selector contract. Runtime
selectors and `selectors/reference-matrix.md` are generated from that map; do
not add a raw selector directly to runtime code or edit a generated file by
hand.

This guide follows the synchronization and evidence policy in epic #589.

## Workflow

1. **Choose a logical ID.** Use a stable, domain-qualified name such as
   `apply_form.APPLY_COVER_LETTER_TEXTAREA`. Describe the UI role, not the
   current wording or an incidental CSS class. Check the map and the selector
   group first so an existing contract is extended instead of duplicated.
2. **Search all three references.** Search the pinned commits in the map for
   the same semantic element. Record the reference path, commit, and exact
   selector in the evidence. Two of three identical values are upstream
   consensus; three of three are an exact reference match.
3. **Make the decision.** Apply the synchronization policy:
   - 2/3 or 3/3 semantic consensus: use the consensus value;
   - one reference: treat it as a candidate and review it;
   - conflicting references: do not choose automatically;
   - no reference: a local selector is allowed only with provenance and an
     explicit reason;
   - a selector proposed only by an LLM is a hypothesis, not an active
     selector (see below).
4. **Add the contract fields.** Add the selector to the canonical map with a
   stable value, its READ/WRITE criticality, the decision, and every required
   provenance and verification field from the template below. Evidence must be
   sanitized: never commit account data, cookies, tokens, or an unredacted
   DOM snapshot.
5. **Regenerate derived files.** Run `python scripts/selector_contracts.py
   render`. Review both the generated runtime and the reference matrix; do not
   hand-edit either output.
6. **Test the reachable flow.** Run the contract checks and the appropriate
   local tests. A selector used by a mutation must also have evidence from the
   real end-to-end flow before it can be active as WRITE.

## Required contract fields

Every selector contract must contain all of these fields. `origin` describes
where the value came from; `verification` describes what was actually
checked. They are independent and neither replaces the other.

| Field | Allowed values / meaning |
| --- | --- |
| `origin` | `reference_exact` (same value in all three references); `reference_consensus` (same value in at least two of three); `reference_single` (one reference); `browser_dom` (read from saved or live browser DOM); `manual` (human-added with an explicit reason); `llm_hypothesis` (LLM proposal without browser or reference confirmation). |
| `verification` | `live_passed` (the corresponding real end-to-end flow passed); `browser_observed` (the element was found in browser DOM, but the full flow was not run); `contract_tested` (only syntax/structure and local contract tests); `unverified` (no factual confirmation); `failed` (the latest check failed); `unavailable` (no safely confirmed selector is available, so the operation is fail-closed). |
| `evidence` | A stable link or repository path to the reference path/commit, sanitized DOM snapshot, test artifact, or run report, plus enough context to identify what was checked. |
| `last_verified_at` | The date/time of the latest factual verification, in an unambiguous ISO 8601 form. Do not update it merely because the map was edited. |
| `verified_flow` | The concrete flow and state checked, for example `apply: open response form, fill letter, submit, verify confirmation`. |
| `verified_by` | `ci`, `browser`, `human`, or the identifier of the tool/agent that performed the check. |

### Evidence template

Copy this shape into a new map entry and replace every placeholder. Keep
`evidence.source` immutable or commit-pinned whenever possible.

```yaml
my_domain.MY_SELECTOR:
  value: "[data-qa='replace-me']"
  criticality: read # read or write; use write when mutation reachability is involved
  decision: consensus # the reviewed map decision, not a substitute for verification
  origin: reference_consensus
  verification: browser_observed
  evidence:
    source: "selectors/evidence/<logical-id>.md"
    note: "Reference commit/path or sanitized DOM/run artifact and what it proves."
  last_verified_at: "2026-08-25T00:00:00Z"
  verified_flow: "Read the concrete page/state and locate the element."
  verified_by: browser
  active: true
```

For a WRITE selector, use `verification: live_passed` only after the real
end-to-end scenario passes, and make the evidence identify that scenario and
its artifact. `browser_observed` is not equivalent to `live_passed`. A
`failed`, `unverified`, or `unavailable` contract must not remain active on a
WRITE path.

Missing `origin` or `verification` is a contract error. If the selector value
changes, reset `verification` to at least `unverified` and collect new
evidence before reactivating it.

## READ and WRITE policy

Criticality is based on reachability, not just on whether the immediate call
looks like a read. A title, status, or container is WRITE-critical when it is
used for mutation identity, scoping, or post-save verification.

| Contract | Policy |
| --- | --- |
| READ | A manual PR is required first. Later `read_auto` updates are permitted only after the soak period, with confirmed 2/3 consensus and green contract tests. |
| WRITE | Manual update only. It requires current evidence from the real end-to-end flow and `verification: live_passed`; WRITE selectors are never auto-updated. |
| Single-source or conflicting reference | Candidate/review only; no automatic choice. |
| No reference | Allowed only with `manual` or `browser_dom` provenance, an explicit reason, and evidence. |

The current policy is `manual`. Do not enable `read_auto` as part of a normal
selector contribution. Keep selectors unavailable/fail-closed when there is
no safe confirmation rather than broadening the selector or guessing.

## `llm_hypothesis` rule

`origin: llm_hypothesis` is never eligible for generated active runtime. It
may remain documented as a candidate/fail-closed contract while reference or
browser evidence is collected. Once confirmed, replace the hypothesis
provenance with the actual origin and record the new verification; do not make
the hypothesis active merely by setting `active: true`.

## Pull request checklist

- [ ] The logical ID is new or the existing contract is intentionally updated;
      no duplicate raw selector was added to runtime code.
- [ ] All three references were searched and the reference paths/commits or
      explicit no-reference rationale are recorded.
- [ ] `origin`, `verification`, `evidence`, `last_verified_at`,
      `verified_flow`, and `verified_by` are present and describe the actual
      state.
- [ ] READ/WRITE criticality reflects mutation reachability; WRITE has
      `live_passed` evidence from a real end-to-end flow.
- [ ] `llm_hypothesis` is not active runtime.
- [ ] `python scripts/selector_contracts.py check` passes.
- [ ] `python scripts/selector_contracts.py render` was run and its generated
      runtime and matrix changes are included when applicable.
- [ ] `ruff check src/ tests/` and the full `pytest` run pass. Follow
      `docs/testing.md` for live-test boundaries; do not run account-changing
      live actions without explicit authorization.
- [ ] The PR explains the decision, evidence, affected flow, and whether the
      selector is READ or WRITE.
